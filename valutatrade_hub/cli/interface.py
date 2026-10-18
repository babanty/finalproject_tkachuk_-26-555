"""Parse commands, compose application services, and present their results."""

import argparse
import shlex
import sys
from collections.abc import Sequence
from contextlib import redirect_stderr, redirect_stdout
from threading import Event
from typing import TextIO

from prettytable import PrettyTable

from valutatrade_hub.cli.composition import ParserRuntime
from valutatrade_hub.core.exceptions import ValidationError, ValutaTradeError
from valutatrade_hub.core.rates import RateService
from valutatrade_hub.core.usecases import TradingService
from valutatrade_hub.core.utils import validate_amount
from valutatrade_hub.infra.database import DatabaseManager
from valutatrade_hub.infra.settings import SettingsLoader
from valutatrade_hub.logging_config import configure_logging, safe_error_message
from valutatrade_hub.parser_service.scheduler import RateScheduler, stop_on_signals
from valutatrade_hub.parser_service.updater import SOURCE_NAMES, UpdateResult


class CommandArgumentError(ValidationError):
    """Distinguish invalid command syntax from an operation failure."""


class CommandParser(argparse.ArgumentParser):
    """Report argument errors without echoing potentially secret input."""

    def error(self, message: str) -> None:
        """Reject invalid syntax without including supplied argument values."""
        raise CommandArgumentError(
            "Некорректные аргументы. Используйте help или --help"
        )


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise ValueError
    return number


def _positive_seconds(value: str) -> float:
    return validate_amount(float(value))


def _print_help(parser: CommandParser, topic: str | None, output: TextIO) -> None:
    if topic is None:
        parser.print_help(file=output)
    else:
        with redirect_stdout(output):
            parser.parse_args([topic, "--help"])


def build_parser(*, interactive: bool = False) -> CommandParser:
    """Create the supported command grammar without initializing storage."""
    parser = CommandParser(
        prog="valutatrade",
        description="ValutaTrade Hub — учебный валютный кошелёк",
        allow_abbrev=False,
    )
    if not interactive:
        parser.add_argument("--data-dir", help="Каталог JSON-данных")
        parser.add_argument("--config", help="Путь к JSON-конфигурации")
    commands = parser.add_subparsers(dest="command")
    for name, description in (
        ("register", "Создать пользователя и пустой портфель"),
        ("login", "Войти в текущем процессе"),
    ):
        command = commands.add_parser(name, help=description, allow_abbrev=False)
        command.add_argument("--username", required=True, help="Имя пользователя")
        command.add_argument("--password", required=True, help="Пароль")
    portfolio = commands.add_parser(
        "show-portfolio", help="Показать портфель", allow_abbrev=False
    )
    portfolio.add_argument("--base", help="Базовая валюта (по умолчанию из настроек)")
    rate = commands.add_parser("get-rate", help="Показать курс", allow_abbrev=False)
    rate.add_argument("--from", dest="from_code", required=True, help="Исходная валюта")
    rate.add_argument("--to", dest="to_code", required=True, help="Целевая валюта")
    deposit = commands.add_parser(
        "deposit", help="Пополнить виртуальный баланс USD", allow_abbrev=False
    )
    deposit.add_argument("--amount", required=True, type=float, help="Сумма в USD")
    for name, description in (
        ("buy", "Купить валюту за USD"),
        ("sell", "Продать валюту за USD"),
    ):
        trade = commands.add_parser(name, help=description, allow_abbrev=False)
        trade.add_argument("--currency", required=True, help="Код валюты")
        trade.add_argument(
            "--amount", required=True, type=float, help="Количество единиц валюты"
        )
    commands.add_parser("logout", help="Завершить сессию")
    update = commands.add_parser(
        "update-rates", help="Обновить курсы через API", allow_abbrev=False
    )
    update.add_argument("--source", choices=tuple(SOURCE_NAMES))
    listing = commands.add_parser(
        "show-rates", help="Показать локальные курсы без сети", allow_abbrev=False
    )
    listing.add_argument("--currency", help="Исходная валюта")
    listing.add_argument("--top", type=_positive_int, help="Самые дорогие криптовалюты")
    listing.add_argument("--base", default="USD", help="База курсов (USD по умолчанию)")
    commands.add_parser("help", help="Показать справку").add_argument(
        "topic", nargs="?"
    )
    commands.add_parser("exit", aliases=["quit"], help="Завершить приложение")
    return parser


def _execute(
    args: argparse.Namespace,
    service: TradingService,
    default_base: str,
    output: TextIO,
    *,
    rates: RateService | None = None,
    runtime: ParserRuntime | None = None,
    errors: TextIO | None = None,
) -> int:
    if args.command == "update-rates":
        if runtime is None:
            raise ValidationError("Обновление курсов не настроено")
        return _print_update(runtime.update(args.source), output, errors or output)
    elif args.command == "show-rates":
        if rates is None:
            raise ValidationError("Просмотр курсов не настроен")
        rows = rates.list_rates(currency=args.currency, top=args.top, base=args.base)
        if not rows:
            print(
                "Нет курсов для выбранных фильтров или локальный кэш пуст. "
                "Выполните update-rates, чтобы загрузить данные.",
                file=output,
            )
        else:
            table = PrettyTable(
                ["Пара", "Курс", "Обновлено (UTC)", "Источник", "Статус"]
            )
            for row in rows:
                quote = row.quote
                table.add_row(
                    [
                        f"{quote.from_code}_{quote.to_code}",
                        f"{quote.rate:.12g}",
                        quote.updated_at.isoformat().replace("+00:00", "Z"),
                        quote.source,
                        "STALE" if row.stale else "OK",
                    ]
                )
            print(table, file=output)
    elif args.command == "register":
        user = service.register(args.username, args.password)
        print(
            f"Пользователь '{user.username}' зарегистрирован (id={user.user_id}). "
            "Выполните login для входа.",
            file=output,
        )
    elif args.command == "login":
        user = service.login(args.username, args.password)
        print(f"Вы вошли как '{user.username}'", file=output)
    elif args.command == "logout":
        service.logout()
        print("Вы вышли из системы", file=output)
    elif args.command == "show-portfolio":
        base = default_base if args.base is None else args.base
        portfolio = service.show_portfolio(base)
        print(
            f"Портфель пользователя '{portfolio.username}' "
            f"(база: {portfolio.base_currency}):",
            file=output,
        )
        if not portfolio.balances:
            print("Портфель пуст", file=output)
        else:
            table = PrettyTable(
                ["Валюта", "Баланс", f"Стоимость ({portfolio.base_currency})"]
            )
            table.align = "r"
            table.align["Валюта"] = "l"
            for wallet in portfolio.wallets:
                table.add_row(
                    [
                        wallet.currency_code,
                        f"{wallet.balance:.12g}",
                        f"{wallet.value:,.12g}",
                    ]
                )
            print(table, file=output)
        print(f"ИТОГО: {portfolio.total:,.2f} {portfolio.base_currency}", file=output)
    elif args.command == "get-rate":
        quote = service.get_rate(args.from_code, args.to_code)
        inverse = quote.inverse()
        timestamp = quote.updated_at.isoformat().replace("+00:00", "Z")
        print(
            f"Курс {quote.from_code}→{quote.to_code}: {quote.rate:.12g} "
            f"(обновлено: {timestamp}; источник: {quote.source})",
            file=output,
        )
        print(
            f"Обратный курс {inverse.from_code}→{inverse.to_code}: {inverse.rate:.12g}",
            file=output,
        )
    elif args.command in ("deposit", "buy", "sell"):
        if args.command == "deposit":
            result = service.deposit(args.amount)
        elif args.command == "buy":
            result = service.buy(args.currency, args.amount)
        else:
            result = service.sell(args.currency, args.amount)
        transaction = result.transaction
        if transaction.action == "DEPOSIT":
            print(
                f"Виртуальный баланс пополнен: {transaction.amount:.12g} USD",
                file=output,
            )

        else:
            operation = (
                "Покупка выполнена"
                if transaction.action == "BUY"
                else "Продажа выполнена"
            )
            print(
                f"{operation}: {transaction.amount:.12g} {transaction.currency_code} "
                f"по курсу {transaction.rate:.12g} USD/{transaction.currency_code}",
                file=output,
            )
            label = (
                "Стоимость покупки"
                if transaction.action == "BUY"
                else "Выручка от продажи"
            )
            print(f"{label}: {transaction.total:,.12g} USD", file=output)
        print("Изменения в портфеле:", file=output)
        for change in result.changes:
            print(
                f"- {change.currency_code}: было {change.before:.12g} "
                f"→ стало {change.after:.12g}",
                file=output,
            )

    return 0


def _print_update(result: UpdateResult, output: TextIO, errors: TextIO) -> int:
    for source in result.success_sources:
        print(f"Источник {source}: OK", file=output)
    for source, message in result.errors.items():
        print(f"Источник {source}: ERROR — {message}", file=errors)
    print(
        f"Обновлено пар: {result.updated_count}; "
        f"последнее обновление: {result.last_refresh or 'нет'}. "
        + (
            "Обновление завершено с ошибками."
            if result.errors
            else "Обновление завершено."
        ),
        file=output,
    )
    return 1 if result.errors else 0


def run_repl(
    service: TradingService,
    *,
    default_base: str,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    rates: RateService | None = None,
    runtime: ParserRuntime | None = None,
) -> int:
    """Process commands until exit or EOF, retaining one in-memory session."""
    parser = build_parser(interactive=True)
    print("ValutaTrade Hub. Справка: help. Завершение: exit.", file=stdout)
    try:
        while True:
            if stdin.isatty():
                print("> ", end="", file=stdout, flush=True)
            line = stdin.readline()
            if not line:
                return 0
            try:
                tokens = shlex.split(line, posix=True)
            except ValueError:
                print("Ошибка: проверьте кавычки в команде", file=stderr)
                continue
            if not tokens:
                continue
            try:
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    args = parser.parse_args(tokens)
                if args.command in ("exit", "quit"):
                    return 0
                if args.command == "help":
                    _print_help(parser, args.topic, stdout)
                else:
                    _execute(
                        args,
                        service,
                        default_base,
                        stdout,
                        rates=rates,
                        runtime=runtime,
                        errors=stderr,
                    )
            except SystemExit:
                continue
            except ValutaTradeError as error:
                print(f"Ошибка: {safe_error_message(error)}", file=stderr)
    finally:
        service.logout()


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run a single command or start an interactive in-memory session."""
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    parser = build_parser()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            args = parser.parse_args(argv)
        if args.command == "help":
            _print_help(parser, args.topic, stdout)
            return 0
        if args.command in ("exit", "quit"):
            return 0
        overrides = {"data_dir": args.data_dir} if args.data_dir is not None else {}
        settings = SettingsLoader(args.config, overrides=overrides)
        database = DatabaseManager(settings)
        database.initialize()
        configure_logging(settings)
        with ParserRuntime(settings, database) as runtime:
            rates = RateService(
                database,
                ttl_seconds=settings.get("rates_ttl_seconds"),
                refresh_callback=runtime.refresh,
            )
            service = TradingService(database, rate_service=rates)
            if args.command is None:
                return run_repl(
                    service,
                    default_base=settings.get("base_currency"),
                    stdin=stdin,
                    stdout=stdout,
                    stderr=stderr,
                    rates=rates,
                    runtime=runtime,
                )
            try:
                return _execute(
                    args,
                    service,
                    settings.get("base_currency"),
                    stdout,
                    rates=rates,
                    runtime=runtime,
                    errors=stderr,
                )
            finally:
                service.logout()
    except SystemExit as error:
        return int(error.code or 0)
    except CommandArgumentError as error:
        print(f"Ошибка: {safe_error_message(error)}", file=stderr)
        return 2
    except ValutaTradeError as error:
        print(f"Ошибка: {safe_error_message(error)}", file=stderr)
        return 1
    except KeyboardInterrupt:
        print("\nРабота завершена", file=stdout)
        return 130


def parser_main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run once or schedule updates without creating overlapping jobs."""
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    parser = CommandParser(prog="valutatrade-parser", allow_abbrev=False)
    parser.add_argument("--data-dir", help="Каталог JSON-данных")
    parser.add_argument("--config", help="Путь к JSON-конфигурации")
    parser.add_argument("--source", choices=tuple(SOURCE_NAMES))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Обновить и завершиться")
    mode.add_argument(
        "--interval",
        type=_positive_seconds,
        default=None,
        help="Пауза CoinGecko; по умолчанию update_interval_seconds (300 секунд)",
    )
    parser.add_argument(
        "--fiat-interval",
        type=_positive_seconds,
        default=86400,
        help="Пауза ExchangeRate после попытки, секунды (86400)",
    )
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            args = parser.parse_args(argv)
        overrides = {"data_dir": args.data_dir} if args.data_dir is not None else {}
        settings = SettingsLoader(args.config, overrides=overrides)
        database = DatabaseManager(settings)
        database.initialize()
        with ParserRuntime(settings, database) as runtime:
            if args.once:
                return _print_update(runtime.update(args.source), stdout, stderr)
            intervals = {
                "coingecko": args.interval
                if args.interval is not None
                else settings.get("update_interval_seconds"),
                "exchangerate": args.fiat_interval,
            }
            if args.source is not None:
                intervals = {args.source: intervals[args.source]}
            stop = Event()
            scheduler = RateScheduler(
                runtime.update,
                intervals,
                stop=stop,
                on_result=lambda result: _print_update(result, stdout, stderr),
                on_error=lambda error: print(
                    f"Ошибка: {safe_error_message(error)}",
                    file=stderr,
                ),
            )
            print("Парсер запущен. Остановка: Ctrl+C.", file=stdout, flush=True)
            with stop_on_signals(stop):
                scheduler.run()
            print("Парсер остановлен", file=stdout)
            return 0
    except SystemExit as error:
        return int(error.code or 0)
    except CommandArgumentError as error:
        print(f"Ошибка: {safe_error_message(error)}", file=stderr)
        return 2
    except ValutaTradeError as error:
        print(f"Ошибка: {safe_error_message(error)}", file=stderr)
        return 1
    except KeyboardInterrupt:
        print("\nПарсер остановлен", file=stdout)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
