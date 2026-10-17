"""Strict, independently testable clients producing currency-to-USD quotes."""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter

import requests

from valutatrade_hub.core.exceptions import ApiRequestError
from valutatrade_hub.core.utils import validate_number
from valutatrade_hub.parser_service.config import ParserConfig


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _provider_time(value: object) -> str | None:
    if value is None:
        return None
    seconds = validate_number(value, field="Время поставщика")
    return datetime.fromtimestamp(seconds, UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class FetchMetadata:
    """Metadata for one fully validated response, ready for history storage."""

    received_at: datetime
    request_ms: float
    status_code: int
    pair_metadata: dict[str, dict]


class BaseApiClient(ABC):
    """Share transport checks while each provider owns its payload contract."""

    source: str

    def __init__(
        self,
        config: ParserConfig | None = None,
        *,
        session: requests.Session | None = None,
        clock: Callable[[], datetime] = _utc_now,
        timer: Callable[[], float] = perf_counter,
    ) -> None:
        self.config = config if config is not None else ParserConfig()
        self._session = session if session is not None else requests.Session()
        self._owns_session = session is None
        self._clock = clock
        self._timer = timer
        self.last_metadata: FetchMetadata | None = None

    @abstractmethod
    def fetch_rates(self) -> dict[str, float]:
        """Fetch all requested pairs or raise without publishing partial results."""

    @abstractmethod
    def _parse(self, payload: dict) -> tuple[dict[str, float], dict[str, dict]]:
        """Validate provider fields and return quotes plus per-pair metadata."""

    def _fetch(self, url: str, *, params: dict, headers: dict) -> dict[str, float]:
        started = self._timer()
        try:
            response = self._session.get(
                url,
                params=params,
                headers=headers,
                timeout=self.config.request_timeout_seconds,
                allow_redirects=False,
            )
        except requests.Timeout:
            raise ApiRequestError(
                f"{self.source}: истекло время ожидания ответа"
            ) from None
        except requests.ConnectionError:
            raise ApiRequestError(
                f"{self.source}: не удалось установить соединение"
            ) from None
        except requests.RequestException:
            raise ApiRequestError(f"{self.source}: ошибка сетевого запроса") from None
        try:
            if response.status_code != 200:
                messages = {
                    401: "неверный или отсутствующий API-ключ",
                    403: "доступ запрещён; проверьте ключ и тариф",
                    429: "превышен лимит запросов; повторите позже",
                }
                reason = messages.get(response.status_code, "неуспешный HTTP-ответ")
                if 500 <= response.status_code <= 599:
                    reason = "сервис временно недоступен"
                raise ApiRequestError(
                    f"{self.source}: HTTP {response.status_code}, {reason}"
                )
            try:
                payload = response.json()
            except (ValueError, requests.RequestException):
                raise ApiRequestError(
                    f"{self.source}: ответ не является корректным JSON"
                ) from None
            try:
                if not isinstance(payload, dict):
                    raise ValueError
                rates, pair_metadata = self._parse(payload)
                received_at = self._clock()
                if (
                    not isinstance(received_at, datetime)
                    or received_at.utcoffset() is None
                ):
                    raise ValueError
                elapsed = (self._timer() - started) * 1000
                if not math.isfinite(elapsed) or elapsed < 0:
                    raise ValueError
                metadata = FetchMetadata(
                    received_at.astimezone(UTC),
                    elapsed,
                    response.status_code,
                    pair_metadata,
                )
            except (KeyError, TypeError, ValueError, OverflowError, OSError):
                raise ApiRequestError(
                    f"{self.source}: некорректные или неполные данные ответа"
                ) from None
            self.last_metadata = metadata
            return rates
        finally:
            response.close()

    def close(self) -> None:
        """Close an internally owned session without closing injected transports."""
        if self._owns_session:
            self._session.close()


class CoinGeckoClient(BaseApiClient):
    """Read Demo simple/price quotes without placing a Demo key into the URL."""

    source = "CoinGecko"

    def fetch_rates(self) -> dict[str, float]:
        """Query configured coin IDs with optional Demo authentication."""
        self.last_metadata = None
        headers = {}
        if self.config.coingecko_api_key:
            headers["x-cg-demo-api-key"] = self.config.coingecko_api_key
        return self._fetch(
            self.config.coingecko_url,
            params={
                "ids": ",".join(
                    self.config.crypto_id_map[code]
                    for code in self.config.crypto_currencies
                ),
                "vs_currencies": "usd",
                "include_last_updated_at": "true",
            },
            headers=headers,
        )

    def _parse(self, payload: dict) -> tuple[dict[str, float], dict[str, dict]]:
        if "error" in payload or "status" in payload:
            raise ApiRequestError(f"{self.source}: поставщик сообщил об ошибке")
        rates, metadata = {}, {}
        for code in self.config.crypto_currencies:
            raw_id = self.config.crypto_id_map[code]
            record = payload[raw_id]
            if not isinstance(record, dict):
                raise ValueError
            pair = f"{code}_USD"
            rates[pair] = validate_number(record["usd"], field="Курс")
            metadata[pair] = {"raw_id": raw_id}
            if "last_updated_at" in record:
                timestamp = _provider_time(record["last_updated_at"])
                if timestamp is None:
                    raise ValueError
                metadata[pair]["provider_updated_at"] = timestamp
        return rates, metadata


class ExchangeRateApiClient(BaseApiClient):
    """Normalize the Standard v6 USD-to-currency response by taking reciprocals."""

    source = "ExchangeRate-API"

    def fetch_rates(self) -> dict[str, float]:
        """Require a key for this provider without affecting other clients."""
        self.last_metadata = None
        key = self.config.exchangerate_api_key
        if not key:
            raise ApiRequestError("ExchangeRate-API: не задан EXCHANGERATE_API_KEY")
        return self._fetch(
            f"{self.config.exchangerate_api_url}/{key}/latest/USD",
            params={},
            headers={},
        )

    def _parse(self, payload: dict) -> tuple[dict[str, float], dict[str, dict]]:
        if payload.get("result") != "success":
            reasons = {
                "invalid-key": "неверный API-ключ",
                "inactive-account": "подтвердите email аккаунта",
                "quota-reached": "исчерпана квота запросов",
                "unsupported-code": "валюта не поддерживается поставщиком",
                "malformed-request": "поставщик отклонил формат запроса",
            }
            error_type = payload.get("error-type")
            reason = (
                reasons.get(error_type, "поставщик сообщил об ошибке")
                if isinstance(error_type, str)
                else "поставщик сообщил об ошибке"
            )
            raise ApiRequestError(f"{self.source}: {reason}")
        if payload["base_code"] != "USD":
            raise ValueError
        conversions = payload["conversion_rates"]
        if not isinstance(conversions, dict):
            raise ValueError
        timestamp = None
        if "time_last_update_unix" in payload:
            timestamp = _provider_time(payload["time_last_update_unix"])
            if timestamp is None:
                raise ValueError
        rates, metadata = {}, {}
        for code in self.config.fiat_currencies:
            quote = validate_number(conversions[code], field="Курс")
            pair = f"{code}_USD"
            rates[pair] = validate_number(1 / quote, field="Обратный курс")
            metadata[pair] = {"base_code": "USD"}
            if timestamp is not None:
                metadata[pair]["provider_updated_at"] = timestamp
        return rates, metadata
