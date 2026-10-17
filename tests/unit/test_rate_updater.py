"""Provider failures, committed diagnostics, and Core-compatible snapshots."""

import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from valutatrade_hub.core.exceptions import (
    ApiRequestError,
    RateUnavailableError,
    StorageError,
    ValidationError,
)
from valutatrade_hub.core.rates import RateService
from valutatrade_hub.infra.settings import SettingsLoader
from valutatrade_hub.logging_config import configure_logging, configure_parser_logging
from valutatrade_hub.parser_service import storage as storage_module
from valutatrade_hub.parser_service.api_clients import FetchMetadata
from valutatrade_hub.parser_service.storage import RateStorage
from valutatrade_hub.parser_service.updater import RatesUpdater

NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)


@pytest.fixture
def clients():
    crypto = Mock(source="CoinGecko")
    crypto.fetch_rates.return_value = {"BTC_USD": 60000, "ETH_USD": 3000}
    crypto.last_metadata = FetchMetadata(
        NOW,
        125,
        200,
        {
            "BTC_USD": {
                "raw_id": "bitcoin",
                "provider_updated_at": "2026-10-18T11:59:00Z",
            },
            "ETH_USD": {"raw_id": "ethereum"},
        },
    )
    fiat = Mock(source="ExchangeRate-API")
    fiat.fetch_rates.return_value = {"EUR_USD": 1.2}
    fiat.last_metadata = FetchMetadata(NOW, 50, 200, {"EUR_USD": {"base_code": "USD"}})
    return {"coingecko": crypto, "exchangerate": fiat}


def contents(database):
    return {
        name: database.get_path(name).read_bytes()
        for name in ("rates", "exchange_rates")
    }


def events():
    path = SettingsLoader().get("log_dir") / "parser.log"
    return [json.loads(line) for line in path.read_text("utf-8").splitlines()]


def test_successful_sources_publish_metadata_and_core_can_read_quotes(
    database, clients
):
    configure_parser_logging(SettingsLoader())
    result = RatesUpdater(RateStorage(database), clients).run_update()
    assert result.success_sources == ("coingecko", "exchangerate")
    assert result.errors == {}
    assert result.updated_count == 3
    history = database.read_document("exchange_rates")
    assert len(history) == 3
    assert history[0]["meta"] == {
        "request_ms": 125,
        "status_code": 200,
        "raw_id": "bitcoin",
        "provider_updated_at": "2026-10-18T11:59:00Z",
    }
    rates = RateService(database, clock=lambda: NOW)
    assert rates.get_rate("BTC", "EUR").rate == 50000
    assert events()[-1]["result"] == "OK"


@pytest.mark.parametrize("failed", ["coingecko", "exchangerate"])
def test_partial_failure_preserves_failed_pairs_and_their_freshness(
    database, clients, failed
):
    updater = RatesUpdater(RateStorage(database), clients)
    updater.run_update()
    initial = database.read_document("rates")
    clients[failed].fetch_rates.side_effect = ApiRequestError("недоступен")
    succeeded = "exchangerate" if failed == "coingecko" else "coingecko"
    clients[succeeded].last_metadata = replace(
        clients[succeeded].last_metadata, received_at=NOW + timedelta(seconds=301)
    )
    result = updater.run_update()
    assert result.success_sources == (succeeded,)
    assert set(result.errors) == {failed}
    pair = "BTC_USD" if failed == "coingecko" else "EUR_USD"
    snapshot = database.read_document("rates")
    assert snapshot["pairs"][pair] == initial["pairs"][pair]
    assert snapshot["last_refresh"] != initial["last_refresh"]
    with pytest.raises(RateUnavailableError):
        RateService(database, clock=lambda: NOW + timedelta(seconds=301)).get_rate(
            pair.split("_")[0], "USD"
        )


def test_total_failure_does_not_write_or_refresh_files(database, clients):
    updater = RatesUpdater(RateStorage(database), clients)
    initial = updater.run_update()
    before = contents(database)
    for client in clients.values():
        client.fetch_rates.side_effect = ApiRequestError("недоступен")
    result = updater.run_update()
    assert result.success_sources == ()
    assert set(result.errors) == set(clients)
    assert result.updated_count == 0
    assert result.last_refresh == initial.last_refresh
    assert contents(database) == before


@pytest.mark.parametrize("source", ["coingecko", "exchangerate"])
def test_source_filter_does_not_request_other_provider(database, clients, source):
    result = RatesUpdater(RateStorage(database), clients).run_update(source)
    assert result.success_sources == (source,)
    for name, client in clients.items():
        assert client.fetch_rates.call_count == (1 if name == source else 0)


@pytest.mark.parametrize("source", ["unknown", "", [], 1])
def test_invalid_source_fails_before_requests_or_writes(database, clients, source):
    before = contents(database)
    with pytest.raises(ValidationError):
        RatesUpdater(RateStorage(database), clients).run_update(source)
    assert all(client.fetch_rates.call_count == 0 for client in clients.values())
    assert contents(database) == before


def test_requests_are_outside_lock_and_concurrent_newer_data_is_reloaded(
    database, clients
):
    storage = RateStorage(database)

    def quote():
        assert not database._lock.is_locked
        RatesUpdater(storage, {"exchangerate": clients["exchangerate"]}).run_update()
        return {"BTC_USD": 60000, "ETH_USD": 3000}

    clients["coingecko"].fetch_rates.side_effect = quote
    RatesUpdater(storage, {"coingecko": clients["coingecko"]}).run_update()
    assert set(storage.read_snapshot()["pairs"]) == {"BTC_USD", "ETH_USD", "EUR_USD"}


def test_metadata_missing_after_fetch_is_an_isolated_source_failure(database, clients):
    clients["coingecko"].last_metadata = None
    result = RatesUpdater(RateStorage(database), clients).run_update()
    assert result.success_sources == ("exchangerate",)
    assert "coingecko" in result.errors
    assert set(database.read_document("rates")["pairs"]) == {"EUR_USD"}


def test_one_invalid_quote_discards_all_measurements_from_that_source(
    database, clients
):
    clients["coingecko"].fetch_rates.return_value["ETH_USD"] = -1
    result = RatesUpdater(RateStorage(database), clients).run_update()
    assert result.success_sources == ("exchangerate",)
    assert len(database.read_document("exchange_rates")) == 1


def test_repeated_measurement_does_not_duplicate_history_or_log_a_price_change(
    database, clients
):
    configure_parser_logging(SettingsLoader())
    updater = RatesUpdater(RateStorage(database), clients)
    updater.run_update()
    before = contents(database)
    result = updater.run_update()
    assert result.updated_count == 0
    assert contents(database) == before
    assert len([event for event in events() if event["event"] == "RATE_CHANGE"]) == 3


def test_changed_price_is_logged_with_previous_value_and_delta_after_commit(
    database, clients
):
    configure_parser_logging(SettingsLoader())
    updater = RatesUpdater(RateStorage(database), clients)
    updater.run_update()
    clients["coingecko"].last_metadata = replace(
        clients["coingecko"].last_metadata, received_at=NOW + timedelta(seconds=1)
    )
    clients["coingecko"].fetch_rates.return_value["BTC_USD"] = 61000
    updater.run_update("coingecko")
    changes = [
        event
        for event in events()
        if event["event"] == "RATE_CHANGE" and event["before"] is not None
    ]
    assert len(changes) == 1
    assert changes[0]["pair"] == "BTC_USD"
    assert changes[0]["before"] == 60000
    assert changes[0]["after"] == 61000
    assert changes[0]["delta"] == 1000
    assert changes[0]["result"] == "OK"


def test_snapshot_failure_is_raised_and_never_logged_as_success(
    database, clients, monkeypatch
):
    configure_parser_logging(SettingsLoader())
    original = storage_module.atomic_write_json

    def fail(path, data):
        if path.name == "rates.json":
            raise StorageError("simulated")
        original(path, data)

    monkeypatch.setattr(storage_module, "atomic_write_json", fail)
    with pytest.raises(StorageError):
        RatesUpdater(RateStorage(database), clients).run_update()
    assert all(event["result"] == "ERROR" for event in events())
    assert len(database.read_document("exchange_rates")) == 3
    assert database.read_document("rates")["pairs"] == {}


def test_unknown_provider_exception_is_isolated_without_leaking_secrets(
    database, clients
):
    configure_parser_logging(SettingsLoader())
    clients["coingecko"].fetch_rates.side_effect = RuntimeError(
        "password-private https://private.test/key"
    )
    result = RatesUpdater(RateStorage(database), clients).run_update()
    text = json.dumps(events()) + repr(result)
    assert "password-private" not in text
    assert "private.test" not in text
    assert result.success_sources == ("exchangerate",)
    assert events()[-1]["result"] == "PARTIAL"


def test_interrupt_is_not_treated_as_a_provider_failure(database, clients):
    clients["coingecko"].fetch_rates.side_effect = KeyboardInterrupt
    before = contents(database)
    with pytest.raises(KeyboardInterrupt):
        RatesUpdater(RateStorage(database), clients).run_update()
    assert contents(database) == before
    clients["exchangerate"].fetch_rates.assert_not_called()


def test_parser_configuration_is_idempotent_and_action_reconfiguration_keeps_it(
    database, clients, monkeypatch
):
    settings = SettingsLoader()
    configure_logging(settings)
    configure_parser_logging(settings)
    configure_parser_logging(settings)
    monkeypatch.setattr(SettingsLoader, "_instance", None)
    configure_logging(
        SettingsLoader(
            overrides={"log_dir": settings.get("log_dir"), "log_level": "DEBUG"}
        )
    )
    RatesUpdater(RateStorage(database), clients).run_update()
    assert len([event for event in events() if event["event"] == "UPDATE"]) == 1
    assert (settings.get("log_dir") / "actions.log").read_text("utf-8") == ""


def test_parser_logging_failure_does_not_change_saved_update(
    database, clients, monkeypatch
):
    configure_parser_logging(SettingsLoader())
    logger = logging.getLogger("valutatrade_hub.parser")
    monkeypatch.setattr(logger, "info", Mock(side_effect=OSError("unavailable log")))
    result = RatesUpdater(RateStorage(database), clients).run_update()
    assert result.updated_count == 3
    assert len(database.read_document("exchange_rates")) == 3


def test_parser_log_rotates_and_reconfiguration_closes_old_file(
    database, clients, monkeypatch, tmp_path
):
    monkeypatch.setattr(SettingsLoader, "_instance", None)
    settings = SettingsLoader(
        overrides={
            "log_dir": tmp_path / "parser logs",
            "log_max_bytes": 600,
            "log_backup_count": 2,
        }
    )
    configure_parser_logging(settings)
    updater = RatesUpdater(RateStorage(database), clients)
    for iteration in range(10):
        updater.run_update()
    paths = list(settings.get("log_dir").glob("parser.log*"))
    assert len(paths) == 3
    for path in paths:
        for line in path.read_text("utf-8").splitlines():
            assert json.loads(line)["event"] in {"UPDATE", "RATE_CHANGE"}
    monkeypatch.setattr(SettingsLoader, "_instance", None)
    configure_parser_logging(
        SettingsLoader(overrides={"log_dir": tmp_path / "new logs"})
    )
    (settings.get("log_dir") / "parser.log").rename(
        settings.get("log_dir") / "moved.log"
    )
