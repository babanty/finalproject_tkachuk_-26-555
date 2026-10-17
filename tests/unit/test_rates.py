"""Rate freshness, snapshot consistency, refresh limits, and numeric boundaries."""

import os
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from valutatrade_hub.core.exceptions import (
    ApiRequestError,
    CurrencyNotFoundError,
    RateUnavailableError,
    StorageError,
    ValidationError,
)
from valutatrade_hub.core.rates import RateQuote, RateService, make_rate_cache
from valutatrade_hub.infra.database import DatabaseManager
from valutatrade_hub.infra.json_io import atomic_write_json
from valutatrade_hub.infra.settings import SettingsLoader

NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)


def write_rates(database, values, *, timestamp=NOW):
    document = {
        "pairs": {
            f"{code}_USD": {
                "rate": value,
                "updated_at": timestamp.isoformat(),
                "source": code,
            }
            for code, value in values.items()
        },
        "last_refresh": NOW.isoformat(),
    }
    with database.locked():
        atomic_write_json(database.get_path("rates"), document)
    return document


@pytest.mark.parametrize(
    ("source", "target", "expected"),
    [
        ("eur", "usd", 1.25),
        ("USD", "EUR", 0.8),
        ("BTC", "EUR", 48000),
        ("EUR", "BTC", 1.25 / 60000),
    ],
)
def test_direct_inverse_and_cross_rates(database, source, target, expected):
    write_rates(database, {"EUR": 1.25, "BTC": 60000})
    quote = RateService(database, clock=lambda: NOW).get_rate(source, target)
    assert quote.rate == pytest.approx(expected)
    assert quote.from_code == source.upper()
    assert quote.to_code == target.upper()
    assert quote.inverse().rate == pytest.approx(1 / expected)
    assert quote.inverse().updated_at == quote.updated_at


@pytest.mark.parametrize("code", ["USD", "EUR", "BTC"])
def test_identity_needs_no_files_but_still_validates_currency(tmp_path, code):
    directory = tmp_path / "not-created"
    database = DatabaseManager(SettingsLoader(overrides={"data_dir": directory}))
    service = RateService(database, clock=lambda: NOW)
    assert service.get_rate(code, code).rate == 1
    with pytest.raises(CurrencyNotFoundError):
        service.get_rate("ABC", "ABC")
    assert not directory.exists()


@pytest.mark.parametrize("age", [299, 300, 301, -1])
def test_ttl_boundary_and_future_timestamp(database, age):
    write_rates(database, {"EUR": 1.25}, timestamp=NOW - timedelta(seconds=age))
    service = RateService(database, clock=lambda: NOW)
    if age == 299:
        assert service.get_rate("EUR", "USD").rate == 1.25
    else:
        with pytest.raises(RateUnavailableError):
            service.get_rate("EUR", "USD")


def test_cache_hits_do_not_extend_quote_lifetime(database, monkeypatch):
    write_rates(database, {"EUR": 1.25})
    clock = Mock(return_value=NOW)
    read = Mock(wraps=database.read_document)
    monkeypatch.setattr(database, "read_document", read)
    service = RateService(database, clock=clock)
    assert service.get_rate("EUR", "USD").rate == 1.25
    clock.return_value += timedelta(seconds=299)
    assert service.get_rate("EUR", "USD").rate == 1.25
    clock.return_value += timedelta(seconds=1)
    with pytest.raises(RateUnavailableError):
        service.get_rate("EUR", "USD")
    assert read.call_count == 1


def test_oldest_leg_and_last_refresh_do_not_hide_staleness(database):
    document = write_rates(database, {"EUR": 1.25, "BTC": 60000})
    older = NOW - timedelta(seconds=200)
    document["pairs"]["EUR_USD"]["updated_at"] = older.isoformat()
    atomic_write_json(database.get_path("rates"), document)
    clock = Mock(return_value=NOW)
    service = RateService(database, clock=clock)
    quote = service.get_rate("BTC", "EUR")
    assert quote.updated_at == older
    assert quote.source == "BTC + EUR"
    clock.return_value += timedelta(seconds=100)
    with pytest.raises(RateUnavailableError) as captured:
        service.get_rate("BTC", "EUR")
    assert captured.value.currencies == frozenset({"EUR"})


def test_cache_detects_atomic_replacement_even_with_restored_mtime(database):
    write_rates(database, {"EUR": 1.25})
    path = database.get_path("rates")
    before = path.stat()
    service = RateService(database, clock=lambda: NOW)
    assert service.get_rate("EUR", "USD").rate == 1.25
    write_rates(database, {"EUR": 1.75})
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert service.get_rate("EUR", "USD").rate == 1.75


def test_cache_invalidation_and_immutable_snapshot(database, monkeypatch):
    write_rates(database, {"EUR": 1.25})
    read = Mock(wraps=database.read_document)
    monkeypatch.setattr(database, "read_document", read)
    load, invalidate = make_rate_cache(database)
    snapshot = load()
    with pytest.raises(TypeError):
        snapshot["EUR"] = None
    with pytest.raises(FrozenInstanceError):
        snapshot["EUR"].rate = 2
    assert load()["EUR"].rate == 1.25
    assert read.call_count == 1
    invalidate()
    assert load()["EUR"].rate == 1.25
    assert read.call_count == 2


def test_deleted_cache_is_not_served_from_memory(database):
    write_rates(database, {"EUR": 1.25})
    service = RateService(database, clock=lambda: NOW)
    service.get_rate("EUR", "USD")
    database.get_path("rates").unlink()
    with pytest.raises(RateUnavailableError):
        service.get_rate("EUR", "USD")


def test_batch_refreshes_once_and_reprices_all_pairs(database):
    write_rates(database, {"BTC": 60000})

    def refresh(currencies):
        assert currencies == frozenset({"EUR"})
        assert not database._lock.is_locked
        write_rates(database, {"BTC": 65000, "EUR": 1.25})

    callback = Mock(side_effect=refresh)
    service = RateService(database, clock=lambda: NOW, refresh_callback=callback)
    quotes = service.get_rates([("BTC", "USD"), ("BTC", "EUR")])
    assert quotes[("BTC", "USD")].rate == 65000
    assert quotes[("BTC", "EUR")].rate == 52000
    callback.assert_called_once()


@pytest.mark.parametrize("refresh", [False, True])
def test_unsuccessful_refresh_is_bounded_and_cannot_return_stale_data(
    database, refresh
):
    write_rates(database, {"EUR": 1.25}, timestamp=NOW - timedelta(seconds=300))
    callback = Mock()
    service = RateService(database, clock=lambda: NOW, refresh_callback=callback)
    with pytest.raises(RateUnavailableError):
        service.get_rate("EUR", "USD", refresh=refresh)
    assert callback.call_count == int(refresh)


def test_callback_error_propagates_and_next_operation_can_recover(database):
    error = ApiRequestError("offline")
    callback = Mock(side_effect=error)
    service = RateService(database, clock=lambda: NOW, refresh_callback=callback)
    with pytest.raises(ApiRequestError) as captured:
        service.get_rate("EUR", "USD")
    assert captured.value is error
    callback.assert_called_once_with(frozenset({"EUR"}))
    write_rates(database, {"EUR": 1.25})
    assert service.get_rate("EUR", "USD").rate == 1.25
    assert callback.call_count == 1


def test_partial_refresh_does_not_produce_partial_batch(database):
    callback = Mock(side_effect=lambda currencies: write_rates(database, {"EUR": 1.25}))
    service = RateService(database, clock=lambda: NOW, refresh_callback=callback)
    with pytest.raises(RateUnavailableError) as captured:
        service.get_rates([("EUR", "USD"), ("BTC", "USD")])
    assert captured.value.currencies == frozenset({"BTC"})
    callback.assert_called_once_with(frozenset({"EUR", "BTC"}))


@pytest.mark.parametrize("content", ["{broken", '{"pairs": [], "last_refresh": null}'])
def test_corrupt_cache_is_not_masked_by_refresh(database, content):
    database.get_path("rates").write_text(content, encoding="utf-8")
    callback = Mock()
    service = RateService(database, clock=lambda: NOW, refresh_callback=callback)
    with pytest.raises(StorageError):
        service.get_rate("EUR", "USD")
    callback.assert_not_called()


def test_batch_uses_one_snapshot_despite_replacement(database, monkeypatch):
    write_rates(database, {"BTC": 60000, "EUR": 1.25})
    original = database.read_document

    def replace_after_read(name):
        document = original(name)
        write_rates(database, {"BTC": 70000, "EUR": 2})
        return document

    monkeypatch.setattr(database, "read_document", replace_after_read)
    service = RateService(database, clock=lambda: NOW)
    quotes = service.get_rates([("BTC", "USD"), ("EUR", "USD")])
    assert [quotes[pair].rate for pair in quotes] == [60000, 1.25]
    assert service.get_rate("EUR", "USD").rate == 2


@pytest.mark.parametrize("rate", [0, -1, True, "1", float("nan"), float("inf")])
def test_quote_rejects_invalid_rates(rate):
    with pytest.raises(ValidationError):
        RateQuote("EUR", "USD", rate, NOW, "test")


@pytest.mark.parametrize("ttl", [0, -1, True, float("inf")])
def test_invalid_ttl_rejected(database, ttl):
    with pytest.raises(ValidationError):
        RateService(database, ttl_seconds=ttl)


def test_timezone_normalization_and_naive_clock_rejection(database):
    local = NOW.astimezone(timezone(timedelta(hours=3)))
    quote = RateQuote("eur", "usd", 1.25, local, "test")
    assert quote.updated_at == NOW
    assert quote.updated_at.tzinfo is UTC
    with pytest.raises(ValidationError):
        RateQuote("EUR", "USD", 1.25, NOW.replace(tzinfo=None), "test")
    with pytest.raises(ValidationError):
        RateService(database, clock=lambda: NOW.replace(tzinfo=None)).get_rate(
            "USD", "USD"
        )


@pytest.mark.parametrize(("source", "target"), [(1e308, 1e-308), (1e-308, 1e308)])
def test_cross_rate_overflow_and_underflow_are_rejected(database, source, target):
    write_rates(database, {"EUR": source, "BTC": target})
    with pytest.raises(ValidationError):
        RateService(database, clock=lambda: NOW).get_rate("EUR", "BTC")


def test_inverse_overflow_is_rejected():
    quote = RateQuote("USD", "BTC", 5e-324, NOW, "test")
    with pytest.raises(ValidationError):
        quote.inverse()


def test_custom_ttl_and_empty_batch(database):
    write_rates(database, {"EUR": 1.25}, timestamp=NOW - timedelta(seconds=60))
    callback = Mock()
    service = RateService(
        database, ttl_seconds=60, clock=lambda: NOW, refresh_callback=callback
    )
    assert service.get_rates([]) == {}
    callback.assert_not_called()
    with pytest.raises(RateUnavailableError):
        service.get_rate("EUR", "USD", refresh=False)
    callback.assert_not_called()


def test_all_currency_codes_validated_before_io_or_refresh(database, monkeypatch):
    callback = Mock()
    read = Mock(side_effect=AssertionError("Unexpected read"))
    monkeypatch.setattr(database, "read_document", read)
    service = RateService(database, refresh_callback=callback)
    with pytest.raises(CurrencyNotFoundError):
        service.get_rates([("EUR", "USD"), ("ABC", "BTC")])
    read.assert_not_called()
    callback.assert_not_called()


def test_refresh_uses_clock_after_callback(database):
    write_rates(database, {"EUR": 1.25}, timestamp=NOW - timedelta(seconds=300))
    clock = Mock(return_value=NOW)

    def slow_refresh(currencies):
        write_rates(database, {"EUR": 1.75})
        clock.return_value = NOW + timedelta(seconds=300)

    callback = Mock(side_effect=slow_refresh)
    service = RateService(database, clock=clock, refresh_callback=callback)
    with pytest.raises(RateUnavailableError):
        service.get_rate("EUR", "USD")
    callback.assert_called_once()
