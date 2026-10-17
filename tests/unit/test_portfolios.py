"""Ownership, wallet collection, and portfolio valuation tests."""

import sys
from unittest.mock import Mock

import pytest

from valutatrade_hub.core.exceptions import (
    ApiRequestError,
    CurrencyNotFoundError,
    ValidationError,
)
from valutatrade_hub.core.models import Portfolio, User, Wallet


def test_add_currency_is_idempotent_and_normalizes_codes(user, rate_provider):
    """Adding an existing currency preserves its wallet and balance."""
    portfolio = Portfolio(1, user=user, rate_provider=rate_provider)
    wallet = portfolio.add_currency("btc")
    wallet.deposit(0.5)
    assert portfolio.add_currency("BTC") is wallet
    assert portfolio.get_wallet("btc").balance == 0.5
    assert len(portfolio.wallets) == 1
    assert portfolio.get_wallet("EUR") is None


def test_wallet_dictionary_is_copied_on_input_and_output(user, rate_provider):
    """Callers cannot add or remove wallets by editing a dictionary copy."""
    wallet = Wallet("USD", 5)
    original = {"USD": wallet}
    portfolio = Portfolio(1, original, user=user, rate_provider=rate_provider)
    original.clear()
    exposed = portfolio.wallets
    exposed.clear()
    assert portfolio.get_wallet("USD") is wallet
    assert portfolio.wallets == {"USD": wallet}


def test_owner_and_identifier_are_read_only(user, rate_provider):
    """A caller cannot transfer an existing portfolio by property assignment."""
    portfolio = Portfolio(1, user=user, rate_provider=rate_provider)
    assert portfolio.user is user
    assert portfolio.user_id == user.user_id
    with pytest.raises(AttributeError):
        portfolio.user = User.create(2, "bob", "secret")
    with pytest.raises(AttributeError):
        portfolio.user_id = 2


def test_mismatched_owner_is_rejected(user, rate_provider):
    """The stored identifier and supplied user must represent the same owner."""
    with pytest.raises(ValidationError, match="Владелец"):
        Portfolio(2, user=user, rate_provider=rate_provider)


def test_multicurrency_valuation_and_non_usd_base(user, rate_provider):
    """All balances contribute in the selected base without changing wallets."""
    portfolio = Portfolio(
        1,
        {
            "USD": Wallet("USD", 100),
            "EUR": Wallet("EUR", 20),
            "BTC": Wallet("BTC", 0.01),
        },
        user=user,
        rate_provider=rate_provider,
    )
    assert portfolio.get_total_value() == pytest.approx(725)
    assert portfolio.get_total_value("eur") == pytest.approx(580)
    assert portfolio.get_wallet("BTC").balance == 0.01


def test_empty_zero_and_same_currency_balances_do_not_request_rates(user):
    """No conversion is needed for zero balances or the base currency."""
    rates = Mock(side_effect=AssertionError("rate lookup must not happen"))
    portfolio = Portfolio(1, user=user, rate_provider=rates)
    assert portfolio.get_total_value() == 0
    portfolio.add_currency("BTC")
    portfolio.add_currency("USD").deposit(5)
    assert portfolio.get_total_value() == 5
    rates.assert_not_called()


def test_unknown_base_is_rejected_even_for_empty_portfolio(user, rate_provider):
    """An empty portfolio must not accept an unsupported display base."""
    portfolio = Portfolio(1, user=user, rate_provider=rate_provider)
    with pytest.raises(CurrencyNotFoundError):
        portfolio.get_total_value("ABC")
    with pytest.raises(CurrencyNotFoundError):
        portfolio.add_currency("ABC")
    assert portfolio.wallets == {}


@pytest.mark.parametrize("rate", [0, -1, True, "1", float("nan"), float("inf")])
def test_invalid_quote_cannot_produce_a_total(user, rate):
    """A malformed provider value cannot be treated as a valid quote."""
    portfolio = Portfolio(
        1, {"BTC": Wallet("BTC", 1)}, user=user, rate_provider=Mock(return_value=rate)
    )
    with pytest.raises(ValidationError):
        portfolio.get_total_value()
    assert portfolio.get_wallet("BTC").balance == 1


def test_missing_rate_error_propagates_without_partial_total(user):
    """The caller must receive the failure rather than an incomplete valuation."""
    error = ApiRequestError("курс недоступен")
    portfolio = Portfolio(
        1,
        {"USD": Wallet("USD", 100), "BTC": Wallet("BTC", 1)},
        user=user,
        rate_provider=Mock(side_effect=error),
    )
    with pytest.raises(ApiRequestError) as captured:
        portfolio.get_total_value()
    assert captured.value is error


@pytest.mark.parametrize("wallets", [[], {"USD": object()}, {"USD": Wallet("EUR")}])
def test_invalid_wallet_collection_is_rejected(user, rate_provider, wallets):
    """Wallet collection keys, values, and currency codes must agree."""
    with pytest.raises(ValidationError):
        Portfolio(1, wallets, user=user, rate_provider=rate_provider)


def test_duplicate_normalized_wallet_keys_are_rejected(user, rate_provider):
    """Case variants must not overwrite one wallet with another silently."""
    with pytest.raises(ValidationError, match="повторно"):
        Portfolio(
            1,
            {"usd": Wallet("USD"), "USD": Wallet("USD")},
            user=user,
            rate_provider=rate_provider,
        )


def test_rate_provider_must_be_callable(user):
    """Valuation requires an explicit callable dependency."""
    with pytest.raises(ValidationError, match="Источник курсов"):
        Portfolio(1, user=user, rate_provider=None)


@pytest.mark.parametrize("rate", [1.0, 2.0])
def test_valuation_overflow_is_reported(user, rate):
    """Overflow in either multiplication or summation cannot yield infinity."""
    portfolio = Portfolio(
        1,
        {
            "BTC": Wallet("BTC", sys.float_info.max),
            "EUR": Wallet("EUR", sys.float_info.max),
        },
        user=user,
        rate_provider=Mock(return_value=rate),
    )
    with pytest.raises(ValidationError):
        portfolio.get_total_value()
