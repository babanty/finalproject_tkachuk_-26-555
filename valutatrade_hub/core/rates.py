"""Snapshot-based exchange rates with per-pair TTL and bounded refresh."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType

from valutatrade_hub.core.currencies import CryptoCurrency, get_currency
from valutatrade_hub.core.exceptions import (
    RateUnavailableError,
    StorageError,
    ValidationError,
)
from valutatrade_hub.core.utils import validate_amount, validate_text
from valutatrade_hub.infra.database import DatabaseManager

type CurrencyPair = tuple[str, str]
type RefreshCallback = Callable[[frozenset[str]], None]
type Clock = Callable[[], datetime]


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValidationError("Время курса должно содержать часовой пояс")
    return value.astimezone(UTC)


@dataclass(frozen=True)
class RateQuote:
    """An immutable positive quote, its oldest observation time, and sources."""

    from_code: str
    to_code: str
    rate: float
    updated_at: datetime
    source: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "from_code", get_currency(self.from_code).code)
        object.__setattr__(self, "to_code", get_currency(self.to_code).code)
        object.__setattr__(self, "rate", validate_amount(self.rate))
        object.__setattr__(self, "updated_at", _utc(self.updated_at))
        object.__setattr__(self, "source", validate_text(self.source, "Источник"))

    def inverse(self) -> RateQuote:
        """Invert this observation without reading another snapshot."""
        return RateQuote(
            self.to_code,
            self.from_code,
            1.0 / self.rate,
            self.updated_at,
            self.source,
        )


def make_rate_cache(
    database: DatabaseManager,
) -> tuple[Callable[[], Mapping[str, RateQuote]], Callable[[], None]]:
    """Cache parsed snapshots in closures; detect replacement under the DB lock."""
    signature = None
    snapshot: Mapping[str, RateQuote] = MappingProxyType({})

    def invalidate() -> None:
        nonlocal signature, snapshot
        signature = None
        snapshot = MappingProxyType({})

    def load() -> Mapping[str, RateQuote]:
        nonlocal signature, snapshot
        with database.locked():
            try:
                status = database.get_path("rates").stat()
            except FileNotFoundError:
                invalidate()
                return snapshot
            except OSError as error:
                raise StorageError("Не удалось проверить файл rates.json") from error
            current = (
                status.st_mtime_ns,
                status.st_size,
                status.st_ino,
                status.st_ctime_ns,
            )
            if signature != current:
                document = database.read_document("rates")
                parsed = {
                    pair.removesuffix("_USD"): RateQuote(
                        pair.removesuffix("_USD"),
                        "USD",
                        record["rate"],
                        datetime.fromisoformat(record["updated_at"]),
                        record["source"],
                    )
                    for pair, record in document["pairs"].items()
                }
                snapshot = MappingProxyType(parsed)
                signature = current
            return snapshot

    return load, invalidate


class RateService:
    """Resolve normalized pairs from one snapshot and one clock reading per batch."""

    def __init__(
        self,
        database: DatabaseManager,
        *,
        ttl_seconds: float = 300,
        clock: Clock | None = None,
        refresh_callback: RefreshCallback | None = None,
    ) -> None:
        self._ttl = validate_amount(ttl_seconds)
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)
        self._refresh = refresh_callback
        self._load, self.invalidate = make_rate_cache(database)

    def list_rates(
        self, *, currency: str | None = None, top: int | None = None, base: str = "USD"
    ) -> tuple[CachedRate, ...]:
        """List one cached snapshot, including stale rows, without any refresh."""
        base = get_currency(base).code
        currency = None if currency is None else get_currency(currency).code
        if top is not None and (type(top) is not int or top <= 0):
            raise ValidationError("Количество курсов должно быть положительным целым")
        snapshot = self._load()
        now = _utc(self._clock())
        required = {code for code in (base, currency) if code not in (None, "USD")}
        missing = required - snapshot.keys()
        if missing:
            raise RateUnavailableError(frozenset(missing))
        codes = sorted(snapshot) if currency is None else [currency]
        rows = []
        for code in codes:
            if top is not None and not isinstance(get_currency(code), CryptoCurrency):
                continue
            legs = [snapshot[leg] for leg in {code, base} if leg != "USD"]
            quote = self._quote((code, base), snapshot, now)
            if legs and code == base:
                quote = snapshot[code]
                quote = RateQuote(code, base, 1.0, quote.updated_at, quote.source)
            stale = any(
                not 0 <= (now - leg.updated_at).total_seconds() < self._ttl
                for leg in legs
            )
            rows.append(CachedRate(quote, stale))
        if top is not None:
            rows.sort(key=lambda row: (-row.quote.rate, row.quote.from_code))
            rows = rows[:top]
        return tuple(rows)

    def get_rate(
        self, from_code: str, to_code: str, *, refresh: bool = True
    ) -> RateQuote:
        """Return a direct, inverse, cross, or identity quote without login."""
        pair = (get_currency(from_code).code, get_currency(to_code).code)
        return self.get_rates([pair], refresh=refresh)[pair]

    def get_rates(
        self, pairs: Iterable[CurrencyPair], *, refresh: bool = True
    ) -> dict[CurrencyPair, RateQuote]:
        """Resolve all pairs together, refreshing unavailable USD legs at most once."""
        normalized = tuple(
            dict.fromkeys(
                (get_currency(source).code, get_currency(target).code)
                for source, target in pairs
            )
        )
        required = frozenset(
            code
            for source, target in normalized
            if source != target
            for code in (source, target)
            if code != "USD"
        )
        for attempt in range(2):
            snapshot = self._load() if required else {}
            now = _utc(self._clock())
            unavailable = frozenset(
                code
                for code in required
                if code not in snapshot
                or not 0
                <= (now - snapshot[code].updated_at).total_seconds()
                < self._ttl
            )
            if unavailable:
                if attempt == 0 and refresh and self._refresh is not None:
                    try:
                        self._refresh(unavailable)
                    finally:
                        self.invalidate()
                    continue
                raise RateUnavailableError(unavailable)
            return {pair: self._quote(pair, snapshot, now) for pair in normalized}
        raise AssertionError("Unreachable refresh state")

    @staticmethod
    def _quote(
        pair: CurrencyPair, snapshot: Mapping[str, RateQuote], now: datetime
    ) -> RateQuote:
        source, target = pair
        if source == target:
            return RateQuote(source, target, 1.0, now, "identity")
        source_rate = 1.0 if source == "USD" else snapshot[source].rate
        target_rate = 1.0 if target == "USD" else snapshot[target].rate
        legs = [snapshot[code] for code in pair if code != "USD"]
        return RateQuote(
            source,
            target,
            source_rate / target_rate,
            min(quote.updated_at for quote in legs),
            " + ".join(sorted({quote.source for quote in legs})),
        )


@dataclass(frozen=True)
class CachedRate:
    """A display-only quote that must not bypass trading freshness checks."""

    quote: RateQuote
    stale: bool
