"""History-first rate persistence using the same lock and writer as Core."""

from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime

from valutatrade_hub.core.exceptions import StorageError
from valutatrade_hub.infra.database import DatabaseManager
from valutatrade_hub.infra.json_io import atomic_write_json, read_json
from valutatrade_hub.infra.serialization import validate_rate_document


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _canonical(record: dict) -> dict:
    validate_rate_document([record], history=True)
    normalized = deepcopy(record)
    normalized.setdefault("meta", {})
    normalized["timestamp"] = (
        _time(record["timestamp"])
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    normalized["id"] = (
        f"{record['from_currency']}_{record['to_currency']}_{normalized['timestamp']}"
    )
    return normalized


@dataclass(frozen=True)
class RateChange:
    """One committed cache change, including the previous price for diagnostics."""

    pair: str
    before: float | None
    after: float
    source: str
    updated_at: str


@dataclass(frozen=True)
class SaveResult:
    """Counts of persisted new measurements and advanced cache pairs."""

    added_count: int
    updated_count: int
    last_refresh: str | None
    changes: tuple[RateChange, ...]


class RateStorage:
    """Merge fresh documents under the repository's inter-process lock."""

    def __init__(self, database: DatabaseManager) -> None:
        self._database = database

    def read_snapshot(self) -> dict:
        """Read the validated snapshot without repairing or writing anything."""
        return self._database.read_document("rates")

    def _history(self) -> list[dict]:
        history = read_json(self._database.get_path("exchange_rates"))
        validate_rate_document(history, history=True)
        return history

    @staticmethod
    def _index(history: list[dict]) -> dict[str, dict]:
        indexed = {}
        for record in history:
            normalized = _canonical(record)
            if normalized["id"] in indexed:
                raise StorageError("Повторяющееся время измерения в истории курсов")
            indexed[normalized["id"]] = normalized
        return indexed

    @staticmethod
    def _merge(
        snapshot: dict, history: list[dict]
    ) -> tuple[dict, tuple[RateChange, ...]]:
        merged = deepcopy(snapshot)
        for record in history:
            pair = f"{record['from_currency']}_{record['to_currency']}"
            current = merged["pairs"].get(pair)
            if current is None or _time(record["timestamp"]) > _time(
                current["updated_at"]
            ):
                merged["pairs"][pair] = {
                    "rate": record["rate"],
                    "updated_at": record["timestamp"],
                    "source": record["source"],
                }
        timestamps = [_time(quote["updated_at"]) for quote in merged["pairs"].values()]
        if snapshot["last_refresh"] is not None:
            timestamps.append(_time(snapshot["last_refresh"]))
        if timestamps:
            latest = max(timestamps)
            if snapshot["last_refresh"] is None or latest > _time(
                snapshot["last_refresh"]
            ):
                merged["last_refresh"] = latest.isoformat(
                    timespec="microseconds"
                ).replace("+00:00", "Z")
        changes = []
        for pair, quote in sorted(merged["pairs"].items()):
            previous = snapshot["pairs"].get(pair)
            if previous != quote:
                changes.append(
                    RateChange(
                        pair,
                        previous["rate"] if previous is not None else None,
                        quote["rate"],
                        quote["source"],
                        quote["updated_at"],
                    )
                )
        validate_rate_document(merged)
        return merged, tuple(changes)

    def save_measurements(self, records: list[dict]) -> SaveResult:
        """Deduplicate a batch, persist history, then advance the shared snapshot."""
        if not isinstance(records, list):
            raise StorageError("Измерения курсов должны быть списком")
        incoming = [_canonical(record) for record in records]
        with self._database.locked():
            snapshot = self.read_snapshot()
            if not incoming:
                return SaveResult(0, 0, snapshot["last_refresh"], ())
            history = self._history()
            indexed = self._index(history)
            combined = deepcopy(history)
            for record in incoming:
                previous = indexed.get(record["id"])
                if previous is not None:
                    if previous != record:
                        raise StorageError("Конфликт идентификатора измерения курса")
                    continue
                indexed[record["id"]] = record
                combined.append(record)
            merged, changes = self._merge(snapshot, combined)
            added_count = len(combined) - len(history)
            if added_count:
                atomic_write_json(self._database.get_path("exchange_rates"), combined)
            if merged != snapshot:
                try:
                    atomic_write_json(self._database.get_path("rates"), merged)
                except StorageError:
                    raise StorageError(
                        "История курсов сохранена, но снимок не обновлён. "
                        "Повторите обновление или восстановите снимок из истории"
                    ) from None
            return SaveResult(
                added_count, len(changes), merged["last_refresh"], changes
            )

    def rebuild_cache_from_history(self) -> SaveResult:
        """Recover committed measurements while preserving valid cache-only pairs."""
        with self._database.locked():
            history = self._history()
            self._index(history)
            damaged = False
            try:
                snapshot = self.read_snapshot()
            except StorageError:
                if not history:
                    raise StorageError(
                        "Нет истории для восстановления снимка"
                    ) from None
                snapshot = {"pairs": {}, "last_refresh": None}
                damaged = True
            merged, changes = self._merge(snapshot, history)
            if damaged or merged != snapshot:
                atomic_write_json(self._database.get_path("rates"), merged)
            return SaveResult(0, len(changes), merged["last_refresh"], changes)
