"""Parser settings and environment credentials, loaded on explicit construction."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from valutatrade_hub.core.currencies import CryptoCurrency, FiatCurrency, get_currency
from valutatrade_hub.core.exceptions import ValidationError
from valutatrade_hub.core.utils import validate_number
from valutatrade_hub.infra.settings import SettingsLoader

CRYPTO_ID_MAP = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana"}
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
EXCHANGERATE_API_URL = "https://v6.exchangerate-api.com/v6"


def _currencies(codes, currency_type) -> tuple[str, ...]:
    if not isinstance(codes, (tuple, list)) or not codes:
        raise ValidationError("Список валют парсера не может быть пустым")
    currencies = [get_currency(code) for code in codes]
    normalized = tuple(currency.code for currency in currencies)
    if (
        any(not isinstance(currency, currency_type) for currency in currencies)
        or "USD" in normalized
        or len(set(normalized)) != len(normalized)
    ):
        raise ValidationError("Некорректный список валют парсера")
    return normalized


def _key(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise ValidationError("Некорректный формат API-ключа")
    return value


def _endpoint(value: str) -> str:
    if not isinstance(value, str):
        raise ValidationError("Некорректный адрес API")
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or any(character.isspace() for character in value)
        ):
            raise ValueError
    except ValueError:
        raise ValidationError(
            "Адрес API должен быть HTTPS URL без credentials"
        ) from None
    return value.rstrip("/")


@dataclass
class ParserConfig:
    """Keep per-instance collections and omit credentials from representations."""

    exchangerate_api_key: str | None = field(
        default_factory=lambda: os.environ.get("EXCHANGERATE_API_KEY"), repr=False
    )
    coingecko_api_key: str | None = field(
        default_factory=lambda: os.environ.get("COINGECKO_API_KEY"), repr=False
    )
    coingecko_url: str = COINGECKO_URL
    exchangerate_api_url: str = EXCHANGERATE_API_URL
    base_currency: str = "USD"
    fiat_currencies: tuple[str, ...] = ("EUR", "GBP", "RUB")
    crypto_currencies: tuple[str, ...] = ("BTC", "ETH", "SOL")
    crypto_id_map: dict[str, str] = field(default_factory=lambda: CRYPTO_ID_MAP.copy())
    request_timeout_seconds: float = 10.0
    rates_file: Path = Path("data/rates.json")
    history_file: Path = Path("data/exchange_rates.json")

    def __post_init__(self) -> None:
        self.exchangerate_api_key = _key(self.exchangerate_api_key)
        self.coingecko_api_key = _key(self.coingecko_api_key)
        self.coingecko_url = _endpoint(self.coingecko_url)
        self.exchangerate_api_url = _endpoint(self.exchangerate_api_url)
        if self.base_currency != "USD":
            raise ValidationError("Базовая валюта парсера должна быть USD")
        self.fiat_currencies = _currencies(self.fiat_currencies, FiatCurrency)
        self.crypto_currencies = _currencies(self.crypto_currencies, CryptoCurrency)
        if not isinstance(self.crypto_id_map, dict):
            raise ValidationError("Сопоставление CoinGecko должно быть словарём")
        for code in self.crypto_currencies:
            identifier = self.crypto_id_map.get(code)
            if (
                not isinstance(identifier, str)
                or re.fullmatch(r"[a-z0-9-]+", identifier) is None
            ):
                raise ValidationError(
                    "Для криптовалюты не задан корректный CoinGecko ID"
                )
        self.crypto_id_map = self.crypto_id_map.copy()
        self.request_timeout_seconds = validate_number(
            self.request_timeout_seconds, field="Таймаут запроса"
        )
        try:
            self.rates_file = Path(self.rates_file).expanduser().resolve()
            self.history_file = Path(self.history_file).expanduser().resolve()
        except (TypeError, ValueError, OSError, RuntimeError):
            raise ValidationError("Некорректные пути данных парсера") from None

    @classmethod
    def from_settings(cls, settings: SettingsLoader) -> ParserConfig:
        """Share Core paths and timeout without loading a second configuration."""
        return cls(
            rates_file=settings.get("rates_file"),
            history_file=settings.get("history_file"),
            request_timeout_seconds=settings.get("request_timeout_seconds"),
        )
