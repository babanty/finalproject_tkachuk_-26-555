"""Currency hierarchy and the registry of supported currencies."""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from valutatrade_hub.core.exceptions import CurrencyNotFoundError
from valutatrade_hub.core.utils import (
    normalize_currency_code,
    validate_number,
    validate_text,
)


@dataclass(frozen=True)
class Currency(ABC):
    """Immutable currency metadata with a common display interface."""

    name: str
    code: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", validate_text(self.name, "Название валюты"))
        object.__setattr__(self, "code", normalize_currency_code(self.code))

    @abstractmethod
    def get_display_info(self) -> str:
        """Return a readable description of the currency."""


@dataclass(frozen=True)
class FiatCurrency(Currency):
    """Fiat currency with its issuing country or region."""

    issuing_country: str

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(
            self,
            "issuing_country",
            validate_text(self.issuing_country, "Страна или регион эмиссии"),
        )

    def get_display_info(self) -> str:
        """Include the issuing country or region in the display text."""
        return f"[FIAT] {self.code} — {self.name} (Issuing: {self.issuing_country})"


@dataclass(frozen=True)
class CryptoCurrency(Currency):
    """Cryptocurrency metadata; a zero market cap means it has not been loaded."""

    algorithm: str
    market_cap: float

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "algorithm", validate_text(self.algorithm, "Алгоритм"))
        object.__setattr__(
            self,
            "market_cap",
            validate_number(
                self.market_cap, field="Объём капитализации", allow_zero=True
            ),
        )

    def get_display_info(self) -> str:
        """Include the algorithm and a compact market capitalization."""
        if self.market_cap == 0:
            capitalization = "N/A"
        else:
            mantissa, exponent = f"{self.market_cap:.2e}".split("e")
            capitalization = f"{mantissa}e{int(exponent)}"
        return (
            f"[CRYPTO] {self.code} — {self.name} "
            f"(Algo: {self.algorithm}, MCAP: {capitalization})"
        )


_CURRENCIES: dict[str, Currency] = {
    "USD": FiatCurrency("US Dollar", "USD", "United States"),
    "EUR": FiatCurrency("Euro", "EUR", "Eurozone"),
    "GBP": FiatCurrency("Pound Sterling", "GBP", "United Kingdom"),
    "RUB": FiatCurrency("Russian Ruble", "RUB", "Russia"),
    "BTC": CryptoCurrency("Bitcoin", "BTC", "SHA-256", 0.0),
    "ETH": CryptoCurrency("Ethereum", "ETH", "Proof of Stake", 0.0),
    "SOL": CryptoCurrency("Solana", "SOL", "PoH / PoS", 0.0),
}


def get_currency(code: str) -> Currency:
    """Look up a currency, distinguishing invalid and unsupported codes."""
    normalized = normalize_currency_code(code)
    try:
        return _CURRENCIES[normalized]
    except KeyError:
        raise CurrencyNotFoundError(normalized) from None
