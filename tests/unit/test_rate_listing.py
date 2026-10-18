"""Read-only listing filters, stale dependencies, and cross rates."""

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from valutatrade_hub.core.exceptions import (
    CurrencyNotFoundError,
    RateUnavailableError,
    StorageError,
    ValidationError,
)
from valutatrade_hub.core.rates import RateService
from valutatrade_hub.infra.json_io import atomic_write_json

NOW = datetime(2026, 10, 18, tzinfo=UTC)


@pytest.fixture
def listing(database):
    document = {
        "pairs": {
            f"{code}_USD": {
                "rate": price,
                "updated_at": NOW.isoformat(),
                "source": code,
            }
            for code, price in {
                "BTC": 60000,
                "ETH": 3000,
                "SOL": 150,
                "EUR": 1.2,
            }.items()
        },
        "last_refresh": NOW.isoformat(),
    }
    atomic_write_json(database.get_path("rates"), document)
    refresh = Mock(side_effect=AssertionError("Listing must never refresh"))
    service = RateService(database, clock=lambda: NOW, refresh_callback=refresh)
    return service, document


@pytest.mark.parametrize(
    "filters, codes",
    [
        ({}, ["BTC", "ETH", "EUR", "SOL"]),
        ({"currency": "btc"}, ["BTC"]),
        ({"top": 2}, ["BTC", "ETH"]),
        ({"top": 8}, ["BTC", "ETH", "SOL"]),
        ({"top": 1, "currency": "sol"}, ["SOL"]),
        ({"top": 1, "currency": "eur"}, []),
        ({"currency": "USD"}, ["USD"]),
    ],
)
def test_filters(listing, filters, codes):
    service, _ = listing
    rows = service.list_rates(**filters)
    assert [row.quote.from_code for row in rows] == codes
    assert all(not row.stale for row in rows)


@pytest.mark.parametrize("age", [300, 301, -1])
@pytest.mark.parametrize("leg", ["BTC", "EUR"])
def test_stale_and_future_legs_are_displayed_not_refreshed(database, listing, age, leg):
    service, document = listing
    document["pairs"][f"{leg}_USD"]["updated_at"] = (
        NOW - timedelta(seconds=age)
    ).isoformat()
    atomic_write_json(database.get_path("rates"), document)
    before = database.get_path("rates").read_bytes()
    (row,) = service.list_rates(currency="BTC", base="eur")
    assert row.quote.rate == 50000
    assert row.stale
    assert row.quote.updated_at == min(NOW, NOW - timedelta(seconds=age))
    assert database.get_path("rates").read_bytes() == before


def test_identity_retains_cached_timestamp_and_stale_status(database, listing):
    service, document = listing
    timestamp = NOW - timedelta(seconds=400)
    document["pairs"]["EUR_USD"]["updated_at"] = timestamp.isoformat()
    atomic_write_json(database.get_path("rates"), document)
    (row,) = service.list_rates(currency="EUR", base="EUR")
    assert row.quote.rate == 1
    assert row.quote.updated_at == timestamp
    assert row.stale


@pytest.mark.parametrize("top", [0, -1, True, 1.5, "2"])
def test_invalid_top(listing, top):
    with pytest.raises(ValidationError):
        listing[0].list_rates(top=top)


@pytest.mark.parametrize("filters", [{"currency": "ABC"}, {"base": "ABC"}])
def test_unknown_currency(listing, filters):
    with pytest.raises(CurrencyNotFoundError):
        listing[0].list_rates(**filters)


@pytest.mark.parametrize("filters", [{"currency": "GBP"}, {"base": "GBP"}])
def test_known_currency_without_quote(listing, filters):
    with pytest.raises(RateUnavailableError):
        listing[0].list_rates(**filters)


def test_empty_missing_and_corrupt_snapshot(database):
    service = RateService(database)
    assert service.list_rates() == ()
    database.get_path("rates").unlink()
    assert service.list_rates() == ()
    database.get_path("rates").write_text("{broken", encoding="utf-8")
    with pytest.raises(StorageError):
        service.list_rates()
