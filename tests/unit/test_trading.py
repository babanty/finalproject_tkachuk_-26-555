"""Trading operations settle both balances and history or change nothing."""

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from valutatrade_hub.core.exceptions import (
    AuthenticationError,
    CurrencyNotFoundError,
    InsufficientFundsError,
    RateUnavailableError,
    StorageError,
    ValidationError,
)
from valutatrade_hub.core.rates import RateService
from valutatrade_hub.core.usecases import TradingService
from valutatrade_hub.infra import json_io
from valutatrade_hub.infra.json_io import atomic_write_json

NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)


@pytest.fixture
def trading(database):
    atomic_write_json(
        database.get_path("rates"),
        {
            "pairs": {
                "BTC_USD": {
                    "rate": 60000,
                    "updated_at": NOW.isoformat(),
                    "source": "test",
                }
            },
            "last_refresh": NOW.isoformat(),
        },
    )
    service = TradingService(
        database, rate_service=RateService(database, clock=lambda: NOW)
    )
    service.register("Алиса", "secret")
    service.login("Алиса", "secret")
    return service


def portfolio(database):
    return database.get_portfolio(1, rate_provider=lambda source, target: 1.0)


def test_funded_buy_and_sell_preserve_total_and_history(database, trading):
    deposit = trading.deposit(10000)
    purchase = trading.buy("btc", 0.05)
    assert portfolio(database).get_wallet("USD").balance == 7000
    assert portfolio(database).get_wallet("BTC").balance == 0.05
    sale = trading.sell("BTC", 0.01)
    saved = portfolio(database)
    assert saved.get_wallet("USD").balance == 7600
    assert saved.get_wallet("BTC").balance == pytest.approx(0.04)
    assert trading.show_portfolio().total == pytest.approx(10000)
    assert [item.action for item in saved.transactions] == ["DEPOSIT", "BUY", "SELL"]
    assert saved.transactions == tuple(
        result.transaction for result in (deposit, purchase, sale)
    )
    assert len({item.id for item in saved.transactions}) == 3
    assert purchase.transaction.total == 3000
    assert sale.transaction.total == 600
    assert [
        (change.currency_code, change.before, change.after)
        for change in purchase.changes
    ] == [
        ("BTC", 0, 0.05),
        ("USD", 10000, 7000),
    ]


@pytest.mark.parametrize("action", ["deposit", "buy", "sell"])
def test_operations_require_login_before_validation_or_quote(database, trading, action):
    trading.logout()
    before = database.get_path("portfolios").read_bytes()
    with pytest.raises(AuthenticationError):
        if action == "deposit":
            trading.deposit(-1)
        else:
            getattr(trading, action)("ABC", -1)
    assert database.get_path("portfolios").read_bytes() == before


@pytest.mark.parametrize(
    "amount", [0, -1, True, "1", None, float("nan"), float("inf"), float("-inf")]
)
@pytest.mark.parametrize("action", ["deposit", "buy", "sell"])
def test_invalid_amounts_do_not_create_wallets_or_history(
    database, trading, action, amount
):
    before = database.get_path("portfolios").read_bytes()
    with pytest.raises(ValidationError):
        if action == "deposit":
            trading.deposit(amount)
        else:
            getattr(trading, action)("BTC", amount)
    assert database.get_path("portfolios").read_bytes() == before
    assert portfolio(database).wallets == {}
    assert portfolio(database).transactions == ()


@pytest.mark.parametrize("action", ["buy", "sell"])
@pytest.mark.parametrize("code", ["USD", "usd", "ABC", "", " BTC"])
def test_invalid_currency_or_self_trade_never_requests_rate(
    database, trading, action, code, monkeypatch
):
    provider = Mock(side_effect=AssertionError("Unexpected quote request"))
    monkeypatch.setattr(trading, "_rate_provider", provider)
    before = database.get_path("portfolios").read_bytes()
    with pytest.raises((ValidationError, CurrencyNotFoundError)):
        getattr(trading, action)(code, 1)
    provider.assert_not_called()
    assert database.get_path("portfolios").read_bytes() == before


@pytest.mark.parametrize("funds", [0, 10])
def test_buy_rejects_insufficient_usd_without_creating_target(database, trading, funds):
    if funds:
        trading.deposit(funds)
    before = database.get_path("portfolios").read_bytes()
    with pytest.raises(InsufficientFundsError) as captured:
        trading.buy("BTC", 1)
    assert captured.value.code == "USD"
    assert captured.value.available == funds
    assert database.get_path("portfolios").read_bytes() == before
    assert portfolio(database).get_wallet("BTC") is None


def test_missing_wallet_and_insufficient_currency_are_distinct(database, trading):
    with pytest.raises(ValidationError, match="нет кошелька 'BTC'"):
        trading.sell("BTC", 1)
    trading.deposit(600)
    trading.buy("BTC", 0.01)
    before = database.get_path("portfolios").read_bytes()
    with pytest.raises(InsufficientFundsError) as captured:
        trading.sell("BTC", 0.02)
    assert captured.value.code == "BTC"
    assert database.get_path("portfolios").read_bytes() == before


def test_full_balance_sale_and_repeated_buy_use_existing_wallet(database, trading):
    trading.deposit(1200)
    trading.buy("BTC", 0.01)
    trading.buy("BTC", 0.01)
    assert portfolio(database).get_wallet("USD").balance == 0
    trading.sell("BTC", portfolio(database).get_wallet("BTC").balance)
    assert portfolio(database).get_wallet("BTC").balance == 0
    assert portfolio(database).get_wallet("USD").balance == 1200
    assert len(portfolio(database).transactions) == 4


@pytest.mark.parametrize("missing", [True, False])
@pytest.mark.parametrize("action", ["buy", "sell"])
def test_missing_and_stale_rates_leave_funded_portfolio_unchanged(
    database, trading, action, missing
):
    trading.deposit(10000)
    trading.buy("BTC", 0.05)
    document = database.read_document("rates")
    if missing:
        document["pairs"] = {}
    else:
        document["pairs"]["BTC_USD"]["updated_at"] = (
            NOW - timedelta(seconds=300)
        ).isoformat()
    atomic_write_json(database.get_path("rates"), document)
    before = database.get_path("portfolios").read_bytes()
    with pytest.raises(RateUnavailableError):
        getattr(trading, action)("BTC", 0.01)
    assert database.get_path("portfolios").read_bytes() == before


@pytest.mark.parametrize("action", ["deposit", "buy", "sell"])
@pytest.mark.parametrize("failure", ["replace", "fsync"])
def test_write_failure_keeps_both_balances_and_history(
    database, trading, action, failure, monkeypatch
):
    trading.deposit(10000)
    trading.buy("BTC", 0.05)
    before = {
        name: database.get_path(name).read_bytes() for name in ("users", "portfolios")
    }

    def fail(*args):
        raise PermissionError("simulated write failure")

    monkeypatch.setattr(json_io.os, failure, fail)
    with pytest.raises(StorageError):
        if action == "deposit":
            trading.deposit(1)
        else:
            getattr(trading, action)("BTC", 0.01)
    assert {name: database.get_path(name).read_bytes() for name in before} == before
    assert len(portfolio(database).transactions) == 2
    assert not list(database.data_dir.glob("*.tmp"))


@pytest.mark.parametrize("rate", [float("inf"), 0, -1, True, "1"])
def test_invalid_provider_quote_cannot_mutate_portfolio(
    database, trading, rate, monkeypatch
):
    monkeypatch.setattr(trading, "_rate_provider", Mock(return_value=rate))
    before = database.get_path("portfolios").read_bytes()
    with pytest.raises(ValidationError):
        trading.buy("BTC", 1)
    assert database.get_path("portfolios").read_bytes() == before


@pytest.mark.parametrize(("amount", "rate"), [(1e308, 60000), (1e-308, 1e-308)])
def test_total_overflow_or_underflow_rejected(
    database, trading, amount, rate, monkeypatch
):
    monkeypatch.setattr(trading, "_rate_provider", Mock(return_value=rate))
    before = database.get_path("portfolios").read_bytes()
    with pytest.raises(ValidationError):
        trading.buy("BTC", amount)
    assert database.get_path("portfolios").read_bytes() == before


@pytest.mark.parametrize("action", ["deposit", "buy", "sell"])
def test_operations_that_float_cannot_represent_are_rejected(database, trading, action):
    trading.deposit(1e20)
    if action == "sell":
        trading.buy("BTC", 1)
    before = database.get_path("portfolios").read_bytes()
    with pytest.raises(ValidationError, match="слишком мала"):
        if action == "deposit":
            trading.deposit(1e-12)
        else:
            getattr(trading, action)("BTC", 1e-12)
    assert database.get_path("portfolios").read_bytes() == before


def test_rate_obtained_before_lock_and_balance_reloaded_after_rate(
    database, trading, monkeypatch
):
    trading.deposit(10000)

    def quote(source, target):
        assert not database._lock.is_locked
        database.update_portfolio(
            1,
            lambda current: current.get_wallet("USD").withdraw(9000),
            rate_provider=lambda source, target: 1,
        )
        return 60000

    monkeypatch.setattr(trading, "_rate_provider", quote)
    with pytest.raises(InsufficientFundsError) as captured:
        trading.buy("BTC", 0.05)
    assert captured.value.available == 1000
    assert portfolio(database).get_wallet("BTC") is None
    assert len(portfolio(database).transactions) == 1


def test_deposit_needs_no_quote_and_sale_can_create_usd_wallet(
    database, trading, monkeypatch
):
    provider = Mock(side_effect=AssertionError("Unexpected quote request"))
    monkeypatch.setattr(trading, "_rate_provider", provider)
    trading.deposit(10)
    provider.assert_not_called()
    database.update_portfolio(
        1,
        lambda current: current.add_currency("BTC").deposit(0.01),
        rate_provider=lambda source, target: 1,
    )
    monkeypatch.setattr(trading, "_rate_provider", lambda source, target: 60000)
    trading.sell("BTC", 0.01)
    assert portfolio(database).get_wallet("USD").balance == 610


def test_sale_of_legacy_crypto_only_portfolio_creates_usd(database, trading):
    database.update_portfolio(
        1,
        lambda current: current.add_currency("BTC").deposit(0.01),
        rate_provider=lambda source, target: 1,
    )
    assert portfolio(database).get_wallet("USD") is None
    trading.sell("BTC", 0.01)
    saved = portfolio(database)
    assert saved.get_wallet("USD").balance == 600
    assert saved.get_wallet("BTC").balance == 0
    assert len(saved.transactions) == 1


@pytest.mark.parametrize("action", ["deposit", "buy", "sell"])
def test_balance_overflow_rejects_entire_operation(
    database, trading, action, monkeypatch
):
    def prepare(current):
        current.add_currency("USD").balance = 1.7e308
        current.add_currency("BTC").balance = 1e308

    database.update_portfolio(1, prepare, rate_provider=lambda source, target: 1)
    monkeypatch.setattr(trading, "_rate_provider", lambda source, target: 1)
    before = database.get_path("portfolios").read_bytes()
    with pytest.raises(ValidationError):
        if action == "deposit":
            trading.deposit(1e308)
        else:
            getattr(trading, action)("BTC", 1e308)
    assert database.get_path("portfolios").read_bytes() == before
