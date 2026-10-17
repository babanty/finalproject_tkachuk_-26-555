"""Collect independent providers before persisting one merged rate batch."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from valutatrade_hub.core.exceptions import (
    ApiRequestError,
    StorageError,
    ValidationError,
)
from valutatrade_hub.core.utils import validate_number
from valutatrade_hub.infra.serialization import validate_rate_document
from valutatrade_hub.logging_config import safe_error_message, safe_text, write_parser
from valutatrade_hub.parser_service.api_clients import BaseApiClient, FetchMetadata
from valutatrade_hub.parser_service.storage import RateStorage

SOURCE_NAMES = {"coingecko": "CoinGecko", "exchangerate": "ExchangeRate-API"}


@dataclass(frozen=True)
class UpdateResult:
    """Report provider outcomes and the number of committed cache changes."""

    success_sources: tuple[str, ...]
    errors: dict[str, str]
    updated_count: int
    last_refresh: str | None


def _measurements(
    source: str, rates: dict, metadata: FetchMetadata | None
) -> list[dict]:
    try:
        if (
            not isinstance(rates, dict)
            or not rates
            or not isinstance(metadata, FetchMetadata)
        ):
            raise ValueError
        received_at = metadata.received_at
        if not isinstance(received_at, datetime) or received_at.utcoffset() is None:
            raise ValueError
        timestamp = (
            received_at.astimezone(UTC)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
        request_ms = validate_number(
            metadata.request_ms, field="Время запроса", allow_zero=True
        )
        if type(metadata.status_code) is not int or metadata.status_code != 200:
            raise ValueError
        if (
            not isinstance(metadata.pair_metadata, dict)
            or metadata.pair_metadata.keys() != rates.keys()
        ):
            raise ValueError
        records = []
        for pair, rate in rates.items():
            from_code, to_code = pair.split("_")
            pair_meta = metadata.pair_metadata[pair]
            if not isinstance(pair_meta, dict):
                raise ValueError
            meta = {"request_ms": request_ms, "status_code": metadata.status_code}
            for key in ("raw_id", "base_code", "provider_updated_at"):
                if key in pair_meta:
                    value = pair_meta[key]
                    if not isinstance(value, str) or not value:
                        raise ValueError
                    meta[key] = safe_text(value)
            records.append(
                {
                    "id": f"{pair}_{timestamp}",
                    "from_currency": from_code,
                    "to_currency": to_code,
                    "rate": rate,
                    "timestamp": timestamp,
                    "source": source,
                    "meta": meta,
                }
            )
        validate_rate_document(records, history=True)
        return records
    except (AttributeError, KeyError, TypeError, ValueError, StorageError):
        raise ApiRequestError(
            f"{source}: некорректные результаты или метаданные клиента"
        ) from None


class RatesUpdater:
    """A failed provider does not block other providers or refresh old pairs."""

    def __init__(
        self, storage: RateStorage, clients: Mapping[str, BaseApiClient]
    ) -> None:
        if (
            not isinstance(clients, Mapping)
            or not clients
            or clients.keys() - SOURCE_NAMES.keys()
        ):
            raise ValidationError("Некорректный набор источников курсов")
        if any(client.source != SOURCE_NAMES[name] for name, client in clients.items()):
            raise ValidationError("Название источника не соответствует клиенту")
        self._storage = storage
        self._clients = dict(clients)

    def run_update(self, source: str | None = None) -> UpdateResult:
        """Fetch outside storage locks and report success only after persistence."""
        if source is not None and (
            not isinstance(source, str) or source not in self._clients
        ):
            raise ValidationError(
                "Неизвестный источник: используйте coingecko или exchangerate"
            )
        selected = self._clients if source is None else {source: self._clients[source]}
        records, successes, errors = [], [], {}
        for name, client in selected.items():
            try:
                rates = client.fetch_rates()
                measurements = _measurements(client.source, rates, client.last_metadata)
            except Exception as error:
                errors[name] = safe_error_message(error)
                write_parser(
                    {
                        "event": "FETCH",
                        "source": name,
                        "result": "ERROR",
                        "error_type": type(error).__name__,
                        "error_message": errors[name],
                    }
                )
            else:
                records.extend(measurements)
                successes.append(name)
        try:
            saved = self._storage.save_measurements(records)
        except Exception as error:
            write_parser(
                {
                    "event": "UPDATE",
                    "result": "ERROR",
                    "error_type": type(error).__name__,
                    "error_message": safe_error_message(error),
                }
            )
            raise
        for change in saved.changes:
            if change.before == change.after:
                continue
            write_parser(
                {
                    "event": "RATE_CHANGE",
                    "result": "OK",
                    "pair": change.pair,
                    "source": safe_text(change.source),
                    "before": change.before,
                    "after": change.after,
                    "delta": None
                    if change.before is None
                    else change.after - change.before,
                    "updated_at": change.updated_at,
                }
            )
        result = UpdateResult(
            tuple(successes), errors, saved.updated_count, saved.last_refresh
        )
        write_parser(
            {
                "event": "UPDATE",
                "result": "PARTIAL"
                if successes and errors
                else "OK"
                if successes
                else "ERROR",
                "success_sources": successes,
                "errors": errors,
                "updated_count": saved.updated_count,
                "added_count": saved.added_count,
                "last_refresh": saved.last_refresh,
            }
        )
        return result
