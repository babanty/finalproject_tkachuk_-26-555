"""Offline CLI composition through real clients, storage, Core, and scheduling."""

import io
import json
import os
import shutil
import signal
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from valutatrade_hub.cli.composition import ParserRuntime
from valutatrade_hub.cli.interface import main, parser_main
from valutatrade_hub.core.exceptions import StorageError
from valutatrade_hub.infra.database import DatabaseManager
from valutatrade_hub.infra.json_io import atomic_write_json
from valutatrade_hub.infra.settings import SettingsLoader
from valutatrade_hub.parser_service.scheduler import RateScheduler


@pytest.fixture
def database(tmp_path):
    settings = SettingsLoader(overrides={"data_dir": str(tmp_path / "данные")})
    repository = DatabaseManager(settings)
    repository.initialize()
    return repository


@pytest.fixture
def transport(monkeypatch):
    calls, failures, sessions = [], set(), []
    monkeypatch.setenv("EXCHANGERATE_API_KEY", "test-key")
    monkeypatch.setenv("COINGECKO_API_KEY", "test-key")

    def request(session, method, url, **kwargs):
        source = "coingecko" if "coingecko" in url else "exchangerate"
        calls.append(source)
        sessions.append(session)
        if source in failures:
            raise requests.Timeout("never print test-key or the URL")
        payload = json.loads(
            (
                Path(__file__).parents[1] / "fixtures/api" / f"{source}_success.json"
            ).read_text(encoding="utf-8")
        )
        response = Mock(spec=requests.Response)
        response.status_code = 200
        response.json.return_value = payload
        return response

    monkeypatch.setattr(requests.Session, "request", request)
    return calls, failures, sessions


def invoke(database, arguments, commands="", *, parser=False):
    output, errors = io.StringIO(), io.StringIO()
    arguments = ["--data-dir", str(database.data_dir), *arguments]
    if parser:
        status = parser_main(arguments, stdout=output, stderr=errors)
    else:
        status = main(
            arguments, stdin=io.StringIO(commands), stdout=output, stderr=errors
        )
    return status, output.getvalue(), errors.getvalue()


@pytest.mark.parametrize("source", [None, "coingecko", "exchangerate"])
@pytest.mark.parametrize("parser", [False, True])
def test_manual_update_and_once_only_call_selected_sources(
    database, transport, source, parser
):
    args = ["--once"] if parser else ["update-rates"]
    if source is not None:
        args += ["--source", source]
    status, output, errors = invoke(database, args, parser=parser)
    assert status == 0
    assert not errors
    assert transport[0] == ([source] if source else ["coingecko", "exchangerate"])
    expected = 3 if source else 6
    assert f"Обновлено пар: {expected}" in output
    assert len(database.read_document("rates")["pairs"]) == expected
    assert len(database.read_document("exchange_rates")) == expected


@pytest.mark.parametrize(
    "failed", [{"coingecko"}, {"exchangerate"}, {"coingecko", "exchangerate"}]
)
@pytest.mark.parametrize("parser", [False, True])
def test_partial_and_total_failure_status_and_persistence(
    database, transport, failed, parser
):
    transport[1].update(failed)
    paths = [database.get_path(name) for name in ("rates", "exchange_rates")]
    before = [path.read_bytes() for path in paths]
    status, output, errors = invoke(
        database, ["--once"] if parser else ["update-rates"], parser=parser
    )
    assert status == 1
    assert "завершено с ошибками" in output
    assert all(f"Источник {source}: ERROR" in errors for source in failed)
    assert "test-key" not in output + errors
    assert transport[0] == ["coingecko", "exchangerate"]
    if len(failed) == 2:
        assert [path.read_bytes() for path in paths] == before
    else:
        assert len(database.read_document("rates")["pairs"]) == 3


@pytest.mark.parametrize(
    "from_code,to_code,expected",
    [
        ("BTC", "USD", ["coingecko"]),
        ("USD", "EUR", ["exchangerate"]),
        ("BTC", "EUR", ["coingecko", "exchangerate"]),
        ("USD", "USD", []),
    ],
)
def test_automatic_refresh_only_once_for_needed_providers(
    database, transport, from_code, to_code, expected
):
    args = ["get-rate", "--from", from_code, "--to", to_code]
    assert invoke(database, args)[0] == 0
    assert transport[0] == expected
    assert invoke(database, args)[0] == 0
    assert transport[0] == expected


def test_stale_refresh_requests_only_stale_leg(database, transport):
    assert invoke(database, ["update-rates"])[0] == 0
    document = database.read_document("rates")
    document["pairs"]["BTC_USD"]["updated_at"] = (
        datetime.now(UTC) - timedelta(seconds=301)
    ).isoformat()
    atomic_write_json(database.get_path("rates"), document)
    transport[0].clear()
    assert invoke(database, ["get-rate", "--from", "BTC", "--to", "EUR"])[0] == 0
    assert transport[0] == ["coingecko"]


def test_failed_automatic_refresh_does_not_retry_or_change_portfolio(
    database, transport
):
    transport[1].add("coingecko")
    status, output, errors = invoke(
        database,
        [],
        "register --username alice --password secret\n"
        "login --username alice --password secret\ndeposit --amount 10000\n"
        "buy --currency BTC --amount 0.05\nshow-portfolio\nquit\n",
    )
    assert status == 0
    assert "Нет актуального курса" in errors
    assert "Покупка выполнена" not in output
    assert "ИТОГО: 10,000.00 USD" in output
    portfolio = database.get_portfolio(
        1, rate_provider=Mock(side_effect=AssertionError("No valuation needed"))
    )
    assert set(portfolio.wallets) == {"USD"}
    assert len(portfolio.transactions) == 1
    assert transport[0] == ["coingecko"]


def test_complete_repl_workflow_and_readonly_show(database, transport, monkeypatch):
    status, output, errors = invoke(
        database,
        [],
        "help get-rate\nregister --username alice --password secret\n"
        "login --username alice --password secret\ndeposit --amount 10000\n"
        "buy --currency BTC --amount 0.05\nsell --currency BTC --amount 0.01\n"
        "show-portfolio\nget-rate --from BTC --to EUR\n"
        "update-rates --source coingecko\nshow-rates --top 2 --base EUR\nquit\n",
    )
    assert status == 0
    assert not errors
    assert "Покупка выполнена" in output and "Продажа выполнена" in output
    assert "ИТОГО: 10,000.00 USD" in output
    assert "BTC_EUR" in output and "ETH_EUR" in output
    assert transport[0] == ["coingecko", "exchangerate", "coingecko"]
    before = database.get_path("rates").read_bytes()
    monkeypatch.setenv("COINGECKO_API_KEY", "invalid key must not matter to listing")
    status, output, errors = invoke(
        database, ["show-rates", "--currency", "BTC", "--top", "1", "--base", "EUR"]
    )
    assert status == 0 and not errors
    assert "BTC_EUR" in output and "ETH_EUR" not in output
    assert database.get_path("rates").read_bytes() == before
    assert len(transport[0]) == 3


def test_show_stale_base_and_missing_base(database, transport):
    assert invoke(database, ["update-rates"])[0] == 0
    document = database.read_document("rates")
    document["pairs"]["EUR_USD"]["updated_at"] = "2000-01-01T00:00:00Z"
    atomic_write_json(database.get_path("rates"), document)
    status, output, errors = invoke(database, ["show-rates", "--base", "EUR"])
    assert status == 0 and not errors
    assert output.count("STALE") == 6
    del document["pairs"]["EUR_USD"]
    atomic_write_json(database.get_path("rates"), document)
    status, output, errors = invoke(database, ["show-rates", "--base", "EUR"])
    assert status == 1 and "EUR_USD" in errors
    assert not output
    assert len(transport[0]) == 2


@pytest.mark.parametrize(
    "args,parser",
    [
        (["show-rates", "--top", "0"], False),
        (["show-rates", "--top", "-2"], False),
        (["show-rates", "--top", "1.2"], False),
        (["update-rates", "--source", "secret"], False),
        (["--interval", "0"], True),
        (["--interval", "nan"], True),
        (["--fiat-interval", "inf"], True),
        (["--once", "--interval", "300"], True),
    ],
)
def test_invalid_syntax_returns_two_without_side_effects(tmp_path, args, parser):
    output, errors = io.StringIO(), io.StringIO()
    function = parser_main if parser else main
    status = function(
        ["--data-dir", str(tmp_path / "unused"), *args], stdout=output, stderr=errors
    )
    assert status == 2
    assert "secret" not in output.getvalue() + errors.getvalue()
    assert not (tmp_path / "unused").exists()


def test_clients_are_lazy_and_all_sessions_closed(database, transport, monkeypatch):
    close = Mock()
    monkeypatch.setattr(requests.Session, "close", close)
    with (
        pytest.raises(StorageError),
        ParserRuntime(SettingsLoader(), database) as runtime,
    ):
        assert not transport[0]
        runtime.update("coingecko")
        raise StorageError("test")
    assert close.call_count == 2


def test_scheduler_entry_point_connects_intervals_and_handles_signal(
    database, transport, monkeypatch
):
    def run(scheduler):
        assert scheduler._intervals == {"coingecko": 15.0, "exchangerate": 86400}
        scheduler._on_result(scheduler._update("coingecko"))
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        assert scheduler._stop.is_set()

    monkeypatch.setattr(RateScheduler, "run", run)
    status, output, errors = invoke(database, ["--interval", "15"], parser=True)
    assert status == 0 and not errors
    assert "Парсер остановлен" in output
    assert transport[0] == ["coingecko"]


def test_once_does_not_enter_scheduler(database, transport, monkeypatch):
    monkeypatch.setattr(
        RateScheduler, "run", Mock(side_effect=AssertionError("must not wait"))
    )
    assert invoke(database, ["--once"], parser=True)[0] == 0


@pytest.mark.parametrize("source", ["coingecko", "exchangerate", None])
def test_scheduler_source_filter_and_config_default(tmp_path, monkeypatch, source):
    monkeypatch.setenv("VALUTATRADE_UPDATE_INTERVAL_SECONDS", "725")
    expected = {"coingecko": 725, "exchangerate": 1000}
    if source:
        expected = {source: expected[source]}

    def run(scheduler):
        assert scheduler._intervals == expected
        scheduler._stop.set()

    monkeypatch.setattr(RateScheduler, "run", run)
    arguments = ["--data-dir", str(tmp_path / "schedule"), "--fiat-interval", "1000"]
    if source:
        arguments += ["--source", source]
    assert parser_main(arguments, stdout=io.StringIO(), stderr=io.StringIO()) == 0


def test_failed_buy_after_existing_deposit_keeps_exact_file(database, transport):
    assert (
        invoke(
            database,
            [],
            "register --username alice --password secret\n"
            "login --username alice --password secret\ndeposit --amount 10000\n",
        )[0]
        == 0
    )
    before = database.get_path("portfolios").read_bytes()
    transport[1].add("coingecko")
    status, output, errors = invoke(
        database,
        [],
        "login --username alice --password secret\nbuy --currency BTC --amount 0.05\n",
    )
    assert status == 0 and "Нет актуального курса" in errors
    assert "Покупка выполнена" not in output
    assert database.get_path("portfolios").read_bytes() == before
    assert transport[0] == ["coingecko"]


def test_missing_fiat_key_does_not_block_crypto(database, transport, monkeypatch):
    monkeypatch.delenv("EXCHANGERATE_API_KEY")
    status, output, errors = invoke(database, ["update-rates"])
    assert status == 1
    assert "Источник coingecko: OK" in output
    assert "Источник exchangerate: ERROR" in errors
    assert transport[0] == ["coingecko"]


@pytest.mark.parametrize(
    "arguments", [["help", "get-rate"], ["help", "show-rates"], ["quit"]]
)
def test_help_topic_and_quit_do_not_initialize_data(tmp_path, arguments):
    output, errors = io.StringIO(), io.StringIO()
    assert (
        main(
            ["--data-dir", str(tmp_path / "unused"), *arguments],
            stdout=output,
            stderr=errors,
        )
        == 0
    )
    assert not errors.getvalue()
    assert not (tmp_path / "unused").exists()


def test_storage_failure_reports_no_success(database, transport, monkeypatch):
    from valutatrade_hub.parser_service import storage

    original = storage.atomic_write_json

    def fail_snapshot(path, data):
        if path == database.get_path("rates"):
            raise StorageError("test failure")
        return original(path, data)

    monkeypatch.setattr(storage, "atomic_write_json", fail_snapshot)
    status, output, errors = invoke(database, ["update-rates"])
    assert status == 1 and "История курсов сохранена" in errors
    assert not output
    assert database.read_document("rates")["pairs"] == {}
    assert len(database.read_document("exchange_rates")) == 6


@pytest.mark.parametrize("console", [False, True])
def test_parser_help_in_separate_process_has_no_side_effects(tmp_path, console):
    executable = shutil.which("valutatrade-parser")
    assert executable
    command = (
        [executable]
        if console
        else [sys.executable, "-m", "valutatrade_hub.parser_service"]
    )
    result = subprocess.run(
        [*command, "--help"],
        cwd=tmp_path,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--once" in result.stdout and "--fiat-interval" in result.stdout
    assert list(tmp_path.iterdir()) == []
