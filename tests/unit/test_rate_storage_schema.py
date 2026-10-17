"""Validate stored rate contracts before the parser is connected."""

from copy import deepcopy

import pytest

from valutatrade_hub.core.exceptions import StorageError
from valutatrade_hub.infra.json_io import atomic_write_json

TIMESTAMP = "2026-10-17T10:00:00Z"


@pytest.fixture
def snapshot():
    """Return a valid educational snapshot, not an actual market quote."""
    return {
        "pairs": {
            "BTC_USD": {"rate": 60000.0, "updated_at": TIMESTAMP, "source": "test"}
        },
        "last_refresh": TIMESTAMP,
    }


def test_valid_rate_snapshot_round_trip(database, snapshot):
    """Snapshot readers retain per-pair source and timestamp metadata."""
    atomic_write_json(database.get_path("rates"), snapshot)
    assert database.read_document("rates") == snapshot


@pytest.mark.parametrize(
    "field,value",
    [
        ("rate", -1),
        ("rate", True),
        ("rate", "60000"),
        ("updated_at", "yesterday"),
        ("updated_at", "2026-10-17"),
        ("source", ""),
    ],
)
def test_bad_quote_fields_are_rejected_without_modification(
    database, snapshot, field, value
):
    """Invalid persisted rates must not be available to valuation consumers."""
    invalid = deepcopy(snapshot)
    invalid["pairs"]["BTC_USD"][field] = value
    path = database.get_path("rates")
    atomic_write_json(path, invalid)
    before = path.read_bytes()
    with pytest.raises(StorageError, match="снимка курсов"):
        database.read_document("rates")
    assert path.read_bytes() == before


@pytest.mark.parametrize("pair", ["btc_USD", "ABC_USD", "USD_EUR", "BTC_USD_EXTRA"])
def test_invalid_pair_keys_are_rejected(database, snapshot, pair):
    """The storage contract only accepts known uppercase currency-to-USD pairs."""
    snapshot["pairs"][pair] = snapshot["pairs"].pop("BTC_USD")
    atomic_write_json(database.get_path("rates"), snapshot)
    with pytest.raises(StorageError):
        database.read_document("rates")


def test_nonempty_snapshot_requires_refresh_timestamp(database, snapshot):
    """An incomplete snapshot is not treated as an initialized empty cache."""
    snapshot["last_refresh"] = None
    atomic_write_json(database.get_path("rates"), snapshot)
    with pytest.raises(StorageError):
        database.read_document("rates")


def test_history_identity_and_duplicate_validation(database):
    """A history entry identifies its pair and observation time uniquely."""
    record = {
        "id": f"BTC_USD_{TIMESTAMP}",
        "from_currency": "BTC",
        "to_currency": "USD",
        "rate": 60000,
        "timestamp": TIMESTAMP,
        "source": "test",
        "meta": {"status_code": 200},
    }
    path = database.get_path("exchange_rates")
    atomic_write_json(path, [record])
    assert database.read_document("exchange_rates") == [record]
    atomic_write_json(path, [record, record])
    with pytest.raises(StorageError):
        database.read_document("exchange_rates")
    record["id"] = "incorrect"
    atomic_write_json(path, [record])
    with pytest.raises(StorageError):
        database.read_document("exchange_rates")
