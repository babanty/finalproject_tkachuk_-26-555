"""Polymorphic currency display, normalization, and registry errors."""

from dataclasses import FrozenInstanceError

import pytest

from valutatrade_hub.core.currencies import (
    CryptoCurrency,
    Currency,
    FiatCurrency,
    get_currency,
)
from valutatrade_hub.core.exceptions import (
    ApiRequestError,
    CurrencyNotFoundError,
    ValidationError,
    ValutaTradeError,
)
from valutatrade_hub.core.utils import normalize_currency_code


def test_currency_is_abstract():
    """Every concrete currency must implement its display method."""
    with pytest.raises(TypeError):
        Currency("US Dollar", "USD")


def test_fiat_and_crypto_follow_assignment_display_format():
    """Different subclasses supply their own information through one interface."""
    fiat = FiatCurrency("US Dollar", "usd", "United States")
    crypto = CryptoCurrency("Bitcoin", "btc", "SHA-256", 1.12e12)
    assert fiat.get_display_info() == "[FIAT] USD — US Dollar (Issuing: United States)"
    assert crypto.get_display_info() == (
        "[CRYPTO] BTC — Bitcoin (Algo: SHA-256, MCAP: 1.12e12)"
    )


def test_registry_has_all_required_codes_and_types():
    """The registry covers both parser sources and normalizes lookup codes."""
    for code in ["USD", "EUR", "GBP", "RUB"]:
        assert isinstance(get_currency(code.lower()), FiatCurrency)
    for code in ["BTC", "ETH", "SOL"]:
        assert isinstance(get_currency(code.lower()), CryptoCurrency)
    assert get_currency("btc").code == "BTC"
    assert "MCAP: N/A" in get_currency("BTC").get_display_info()


@pytest.mark.parametrize(
    "code", ["", "A", "ABCDEF", "U SD", " USD", "USD ", "U$D", "РУБ", "uſd", None, 123]
)
def test_malformed_codes_raise_validation_error(code):
    """Invalid code syntax is distinguished from an unsupported valid code."""
    with pytest.raises(ValidationError):
        get_currency(code)


def test_unknown_code_has_distinct_error_and_normalized_context():
    """A well-formed unsupported code can be reported separately by CLI."""
    with pytest.raises(
        CurrencyNotFoundError, match="Неизвестная валюта 'ABC'"
    ) as error:
        get_currency("abc")
    assert error.value.code == "ABC"
    assert normalize_currency_code("x1") == "X1"


@pytest.mark.parametrize("name", ["", " \t", None, 123])
def test_currency_name_must_be_nonempty_text(name):
    """Currency descriptions cannot omit their human-readable name."""
    with pytest.raises(ValidationError, match="Название валюты"):
        FiatCurrency(name, "USD", "United States")


def test_required_subclass_metadata_is_validated():
    """Fiat regions and cryptocurrency algorithms must be meaningful text."""
    with pytest.raises(ValidationError, match="эмиссии"):
        FiatCurrency("US Dollar", "USD", " ")
    with pytest.raises(ValidationError, match="Алгоритм"):
        CryptoCurrency("Bitcoin", "BTC", "", 1)


@pytest.mark.parametrize("market_cap", [-1, True, "1", float("nan"), float("inf")])
def test_invalid_market_cap_is_rejected(market_cap):
    """Capitalization cannot contain malformed or non-finite numbers."""
    with pytest.raises(ValidationError):
        CryptoCurrency("Bitcoin", "BTC", "SHA-256", market_cap)


def test_registry_currency_cannot_be_mutated():
    """Shared registry metadata cannot be corrupted by a caller."""
    currency = get_currency("USD")
    with pytest.raises(FrozenInstanceError):
        currency.code = "EUR"
    assert get_currency("USD").code == "USD"


def test_api_error_follows_domain_error_contract():
    """Provider errors share the application base type and readable message."""
    error = ApiRequestError("источник недоступен")
    assert isinstance(error, ValutaTradeError)
    assert error.reason == "источник недоступен"
    assert str(error) == "Ошибка при обращении к внешнему API: источник недоступен"
