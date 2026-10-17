"""Rate history survives failed snapshot writes and concurrent-safe retries."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from valutatrade_hub.core.exceptions import StorageError
from valutatrade_hub.infra import json_io
from valutatrade_hub.infra.json_io import atomic_write_json
from valutatrade_hub.parser_service import storage as storage_module
from valutatrade_hub.parser_service.storage import RateStorage

NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)


def measurement(pair="BTC_USD", rate=60000, seconds=0, source="CoinGecko"):
    timestamp = (
        (NOW + timedelta(seconds=seconds))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    from_code, to_code = pair.split("_")
    return {
        "id": f"{pair}_{timestamp}",
        "from_currency": from_code,
        "to_currency": to_code,
        "rate": rate,
        "timestamp": timestamp,
        "source": source,
        "meta": {"request_ms": 12.5, "status_code": 200},
    }


def documents(database):
    return {
        name: database.get_path(name).read_bytes()
        for name in ("rates", "exchange_rates")
    }


def test_history_and_snapshot_round_trip_without_mutating_input(database):
    records = [measurement(), measurement("EUR_USD", 1.25, source="ExchangeRate-API")]
    original = deepcopy(records)
    storage = RateStorage(database)
    result = storage.save_measurements(records)
    assert result.added_count == result.updated_count == 2
    assert result.last_refresh == records[0]["timestamp"]
    assert database.read_document("exchange_rates") == records
    assert storage.read_snapshot()["pairs"]["EUR_USD"] == {
        "rate": 1.25,
        "source": "ExchangeRate-API",
        "updated_at": records[1]["timestamp"],
    }
    assert records == original
    assert [change.pair for change in result.changes] == ["BTC_USD", "EUR_USD"]


def test_duplicate_batch_and_retry_do_not_write_again(database, monkeypatch):
    storage = RateStorage(database)
    record = measurement()
    assert storage.save_measurements([record, deepcopy(record)]).added_count == 1
    before = documents(database)
    writer = Mock(side_effect=AssertionError("Unexpected write"))
    monkeypatch.setattr(storage_module, "atomic_write_json", writer)
    result = storage.save_measurements([record])
    assert result.added_count == result.updated_count == 0
    assert storage.rebuild_cache_from_history().updated_count == 0
    writer.assert_not_called()
    assert documents(database) == before


def test_equivalent_utc_instants_share_one_measurement_identifier(database):
    storage = RateStorage(database)
    record = measurement()
    storage.save_measurements([record])
    same = deepcopy(record)
    same["timestamp"] = "2026-10-18T15:00:00+03:00"
    same["id"] = f"BTC_USD_{same['timestamp']}"
    assert storage.save_measurements([same]).added_count == 0
    assert len(database.read_document("exchange_rates")) == 1


@pytest.mark.parametrize(
    "field,value", [("rate", 70000), ("source", "Other"), ("meta", {"request_ms": 55})]
)
def test_conflicting_identifier_rejects_entire_batch_before_any_write(
    database, field, value
):
    storage = RateStorage(database)
    record = measurement()
    storage.save_measurements([record])
    before = documents(database)
    conflicting = deepcopy(record)
    conflicting[field] = value
    with pytest.raises(StorageError, match="Конфликт"):
        storage.save_measurements([measurement("EUR_USD", 1.2), conflicting])
    assert documents(database) == before


def test_older_measurement_is_historical_without_replacing_newer_quote(database):
    storage = RateStorage(database)
    newest = measurement(rate=61000, seconds=10)
    storage.save_measurements([newest])
    before = database.get_path("rates").read_bytes()
    result = storage.save_measurements([measurement()])
    assert result.added_count == 1
    assert result.updated_count == 0
    assert result.last_refresh == newest["timestamp"]
    assert database.get_path("rates").read_bytes() == before
    assert len(database.read_document("exchange_rates")) == 2


def test_conflicting_duplicates_in_first_batch_do_not_write_anything(database):
    before = documents(database)
    with pytest.raises(StorageError, match="Конфликт"):
        RateStorage(database).save_measurements(
            [measurement(), measurement(rate=61000)]
        )
    assert documents(database) == before


def test_persisted_equivalent_time_duplicates_require_repair_without_overwrite(
    database,
):
    first = measurement()
    second = deepcopy(first)
    second["timestamp"] = "2026-10-18T15:00:00+03:00"
    second["id"] = f"BTC_USD_{second['timestamp']}"
    atomic_write_json(database.get_path("exchange_rates"), [first, second])
    before = documents(database)
    with pytest.raises(StorageError, match="Повторяющееся время"):
        RateStorage(database).rebuild_cache_from_history()
    assert documents(database) == before


def test_equal_timestamp_does_not_replace_a_cache_only_pair(database):
    storage = RateStorage(database)
    record = measurement(rate=61000)
    snapshot = {
        "pairs": {
            "BTC_USD": {
                "rate": 60000,
                "source": "seed",
                "updated_at": record["timestamp"],
            }
        },
        "last_refresh": record["timestamp"],
    }
    atomic_write_json(database.get_path("rates"), snapshot)
    result = storage.save_measurements([record])
    assert result.updated_count == 0
    assert storage.read_snapshot() == snapshot


def test_new_measurement_keeps_other_pair_timestamp_and_records_actual_delta(database):
    storage = RateStorage(database)
    first = measurement()
    storage.save_measurements([first, measurement("EUR_USD", 1.2)])
    result = storage.save_measurements([measurement(rate=61000, seconds=10)])
    snapshot = storage.read_snapshot()
    assert snapshot["pairs"]["EUR_USD"]["updated_at"] == first["timestamp"]
    assert result.updated_count == 1
    assert (result.changes[0].before, result.changes[0].after) == (60000, 61000)


@pytest.mark.parametrize("failed_document", ["exchange_rates.json", "rates.json"])
@pytest.mark.parametrize("failure", ["replace", "fsync"])
def test_real_write_failures_preserve_history_first_recovery(
    database, monkeypatch, failed_document, failure
):
    storage = RateStorage(database)
    storage.save_measurements([measurement(seconds=-10)])
    before = documents(database)
    record = measurement(rate=61000)
    original = getattr(json_io.os, failure)
    calls = 0

    def fail(*args):
        nonlocal calls
        calls += 1
        should_fail = (
            args[1].name == failed_document
            if failure == "replace"
            else calls == (1 if failed_document == "exchange_rates.json" else 2)
        )
        if should_fail:
            raise PermissionError("simulated failure")
        return original(*args)

    with monkeypatch.context() as patch:
        patch.setattr(json_io.os, failure, fail)
        with pytest.raises(StorageError):
            storage.save_measurements([record])
    assert database.get_path("rates").read_bytes() == before["rates"]
    assert not list(database.data_dir.glob("*.tmp"))
    if failed_document == "exchange_rates.json":
        assert documents(database) == before
    else:
        assert len(database.read_document("exchange_rates")) == 2
        result = storage.rebuild_cache_from_history()
        assert result.updated_count == 1
        assert storage.read_snapshot()["pairs"]["BTC_USD"]["rate"] == 61000


def test_next_success_repairs_previous_pending_history_without_duplication(
    database, monkeypatch
):
    storage = RateStorage(database)
    original = storage_module.atomic_write_json

    def fail_snapshot(path, data):
        if path.name == "rates.json":
            raise StorageError("simulated snapshot failure")
        original(path, data)

    with monkeypatch.context() as patch:
        patch.setattr(storage_module, "atomic_write_json", fail_snapshot)
        with pytest.raises(StorageError, match="История курсов сохранена"):
            storage.save_measurements([measurement()])
    pending = documents(database)
    storage.save_measurements([])
    assert documents(database) == pending
    result = storage.save_measurements(
        [measurement(), measurement("EUR_USD", 1.2, seconds=10)]
    )
    assert result.added_count == 1
    assert result.updated_count == 2
    assert len(database.read_document("exchange_rates")) == 2
    assert set(storage.read_snapshot()["pairs"]) == {"BTC_USD", "EUR_USD"}


@pytest.mark.parametrize("damage", ["missing", "invalid_json", "invalid_schema"])
def test_explicit_rebuild_recovers_a_damaged_snapshot(database, damage):
    storage = RateStorage(database)
    storage.save_measurements([measurement()])
    path = database.get_path("rates")
    history = database.get_path("exchange_rates").read_bytes()
    if damage == "missing":
        path.unlink()
    else:
        path.write_text(
            "{broken" if damage == "invalid_json" else "[]", encoding="utf-8"
        )
    result = storage.rebuild_cache_from_history()
    assert result.updated_count == 1
    assert storage.read_snapshot()["pairs"]["BTC_USD"]["rate"] == 60000
    assert database.get_path("exchange_rates").read_bytes() == history


def test_rebuild_without_history_does_not_destroy_damaged_snapshot(database):
    storage = RateStorage(database)
    path = database.get_path("rates")
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(StorageError, match="Нет истории"):
        storage.rebuild_cache_from_history()
    assert path.read_text("utf-8") == "{broken"


def test_rebuild_preserves_valid_cache_only_pairs(database):
    storage = RateStorage(database)
    storage.save_measurements([measurement()])
    snapshot = storage.read_snapshot()
    snapshot["pairs"]["EUR_USD"] = {
        "rate": 1.2,
        "source": "seed",
        "updated_at": measurement()["timestamp"],
    }
    atomic_write_json(database.get_path("rates"), snapshot)
    assert storage.rebuild_cache_from_history().updated_count == 0
    assert storage.read_snapshot() == snapshot


@pytest.mark.parametrize("method", ["save", "rebuild"])
def test_corrupt_history_is_not_silently_replaced(database, method):
    path = database.get_path("exchange_rates")
    path.write_text("{broken", encoding="utf-8")
    before = documents(database)
    storage = RateStorage(database)
    with pytest.raises(StorageError):
        if method == "save":
            storage.save_measurements([measurement()])
        else:
            storage.rebuild_cache_from_history()
    assert documents(database) == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("rate", 0),
        ("rate", True),
        ("id", "bad"),
        ("timestamp", "2026-10-18"),
        ("from_currency", "ABC"),
        ("meta", []),
    ],
)
def test_invalid_measurement_never_changes_files(database, field, value):
    record = measurement()
    record[field] = value
    before = documents(database)
    with pytest.raises(StorageError):
        RateStorage(database).save_measurements([record])
    assert documents(database) == before
