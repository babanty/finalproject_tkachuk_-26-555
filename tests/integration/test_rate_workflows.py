"""Portfolio valuation and public rate commands use real temporary snapshots."""

import io
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from valutatrade_hub.cli.interface import run_repl
from valutatrade_hub.core.exceptions import RateUnavailableError, ValidationError
from valutatrade_hub.core.models import Portfolio, User, Wallet
from valutatrade_hub.core.rates import RateService
from valutatrade_hub.core.usecases import TradingService
from valutatrade_hub.infra.json_io import atomic_write_json

NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)


@pytest.fixture
def market(database):
    snapshot = {
        "pairs": {
            f"{code}_USD": {
                "rate": rate,
                "updated_at": NOW.isoformat(),
                "source": "test",
            }
            for code, rate in {"BTC": 60000, "EUR": 1.25}.items()
        },
        "last_refresh": NOW.isoformat(),
    }
    atomic_write_json(database.get_path("rates"), snapshot)
    user = User.create(1, "Алиса", "secret")
    portfolio = Portfolio(
        1,
        {
            "USD": Wallet("USD", 100),
            "EUR": Wallet("EUR", 200),
            "BTC": Wallet("BTC", 0.05),
            "SOL": Wallet("SOL"),
        },
        user=user,
        rate_provider=lambda source, target: 1.0,
    )
    database.save_account(user, portfolio)
    return snapshot


def make_service(database, **kwargs):
    service = TradingService(
        database, rate_service=RateService(database, clock=lambda: NOW, **kwargs)
    )
    service.login("Алиса", "secret")
    return service


@pytest.mark.parametrize(
    ("base", "expected", "values"),
    [
        ("USD", 3350, {"BTC": 3000, "EUR": 250, "SOL": 0, "USD": 100}),
        ("EUR", 2680, {"BTC": 2400, "EUR": 200, "SOL": 0, "USD": 80}),
    ],
)
def test_valuation_rows_and_total_share_one_snapshot(
    database, market, base, expected, values, monkeypatch
):
    service = make_service(database)
    read = Mock(wraps=database.read_document)
    monkeypatch.setattr(database, "read_document", read)
    before = database.get_path("portfolios").read_bytes()
    view = service.show_portfolio(base)
    assert view.total == pytest.approx(expected)
    assert {wallet.currency_code: wallet.value for wallet in view.wallets} == values
    assert sum(wallet.value for wallet in view.wallets) == pytest.approx(view.total)
    read.assert_called_once_with("rates")
    assert database.get_path("portfolios").read_bytes() == before


def test_valuation_refresh_recalculates_every_wallet(database, market):
    del market["pairs"]["EUR_USD"]
    atomic_write_json(database.get_path("rates"), market)

    def refresh(currencies):
        assert currencies == frozenset({"EUR"})
        market["pairs"]["EUR_USD"] = {
            "rate": 2,
            "updated_at": NOW.isoformat(),
            "source": "test",
        }
        market["pairs"]["BTC_USD"]["rate"] = 70000
        atomic_write_json(database.get_path("rates"), market)

    callback = Mock(side_effect=refresh)
    view = make_service(database, refresh_callback=callback).show_portfolio()
    assert view.total == 4000
    assert {wallet.currency_code: wallet.value for wallet in view.wallets}[
        "BTC"
    ] == 3500
    callback.assert_called_once()


def test_missing_leg_fails_whole_portfolio_without_changes(database, market):
    del market["pairs"]["EUR_USD"]
    atomic_write_json(database.get_path("rates"), market)
    before = database.get_path("portfolios").read_bytes()
    with pytest.raises(RateUnavailableError):
        make_service(database).show_portfolio()
    assert database.get_path("portfolios").read_bytes() == before


def test_base_only_and_zero_balances_need_no_rates(database, saved_account):
    database.update_portfolio(
        1,
        lambda portfolio: portfolio.add_currency("BTC"),
        rate_provider=lambda source, target: 1.0,
    )
    database.get_path("rates").unlink()
    callback = Mock()
    view = make_service(database, refresh_callback=callback).show_portfolio()
    assert view.total == 10
    assert {wallet.currency_code: wallet.value for wallet in view.wallets} == {
        "BTC": 0,
        "USD": 10,
    }
    callback.assert_not_called()


def test_overflow_in_wallet_valuation_has_no_result(database, market):
    database.update_portfolio(
        1,
        lambda portfolio: setattr(portfolio.get_wallet("BTC"), "balance", 1e308),
        rate_provider=lambda source, target: 1.0,
    )
    with pytest.raises(ValidationError):
        make_service(database).show_portfolio()


def test_repl_formats_rates_and_portfolio_and_recovers_from_bad_currency(
    database, market
):
    service = TradingService(
        database, rate_service=RateService(database, clock=lambda: NOW)
    )
    output, errors = io.StringIO(), io.StringIO()
    result = run_repl(
        service,
        default_base="USD",
        stdin=io.StringIO(
            "get-rate --from btc --to eur\nget-rate --from ABC --to ABC\n"
            "get-rate --from USD\nlogin --username Алиса --password secret\n"
            "show-portfolio --base EUR\nexit\n"
        ),
        stdout=output,
        stderr=errors,
    )
    assert result == 0
    text = output.getvalue()
    assert "Курс BTC→EUR: 48000" in text
    assert "Обратный курс EUR→BTC:" in text
    assert "2026-10-17T12:00:00Z" in text
    assert "Стоимость (EUR)" in text
    assert "ИТОГО: 2,680.00 EUR" in text
    assert "Неизвестная валюта 'ABC'" in errors.getvalue()
    assert "Некорректные аргументы" in errors.getvalue()


def test_expired_portfolio_prints_no_partial_table_or_total(database, market):
    market["pairs"]["EUR_USD"]["updated_at"] = (
        NOW - timedelta(seconds=300)
    ).isoformat()
    atomic_write_json(database.get_path("rates"), market)
    output, errors = io.StringIO(), io.StringIO()
    run_repl(
        make_service(database),
        default_base="USD",
        stdin=io.StringIO("show-portfolio\n"),
        stdout=output,
        stderr=errors,
    )
    assert "EUR_USD" in errors.getvalue()
    assert "ИТОГО" not in output.getvalue()
    assert "Баланс" not in output.getvalue()


@pytest.mark.parametrize("fresh", [True, False])
def test_rate_command_works_in_separate_process_without_login(
    database, market, tmp_path, fresh
):
    timestamp = datetime.now(UTC) - timedelta(seconds=0 if fresh else 600)
    for record in market["pairs"].values():
        record["updated_at"] = timestamp.isoformat()
    atomic_write_json(database.get_path("rates"), market)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "valutatrade_hub",
            "--data-dir",
            str(database.data_dir),
            "get-rate",
            "--from",
            "USD",
            "--to",
            "EUR",
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        check=False,
    )
    assert result.returncode == (0 if fresh else 1)
    if fresh:
        assert "Курс USD→EUR: 0.8" in result.stdout
        assert "Обратный курс EUR→USD: 1.25" in result.stdout
        assert not result.stderr
    else:
        assert "Нет актуального курса" in result.stderr
        assert not result.stdout
