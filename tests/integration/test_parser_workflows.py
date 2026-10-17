"""Parser output is readable by Core and concurrent processes retain history."""

import json
import multiprocessing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from valutatrade_hub.core.rates import RateService
from valutatrade_hub.core.usecases import TradingService
from valutatrade_hub.infra.database import DatabaseManager
from valutatrade_hub.infra.settings import SettingsLoader
from valutatrade_hub.parser_service.api_clients import (
    CoinGeckoClient,
    ExchangeRateApiClient,
)
from valutatrade_hub.parser_service.config import ParserConfig
from valutatrade_hub.parser_service.storage import RateStorage
from valutatrade_hub.parser_service.updater import RatesUpdater

NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)


def test_real_client_parsers_publish_quotes_used_by_trading_and_restart(database):
    config = ParserConfig(exchangerate_api_key="test-key", coingecko_api_key="test-key")
    clients = {}
    for name, client_type in (
        ("coingecko", CoinGeckoClient),
        ("exchangerate", ExchangeRateApiClient),
    ):
        payload = json.loads(
            (
                Path(__file__).parents[1] / "fixtures" / "api" / f"{name}_success.json"
            ).read_text("utf-8")
        )
        response = Mock(spec=requests.Response)
        response.status_code = 200
        response.json.return_value = payload
        session = Mock(spec=requests.Session)
        session.get.return_value = response
        clients[name] = client_type(
            config, session=session, clock=lambda: NOW, timer=lambda: 1.0
        )
    result = RatesUpdater(RateStorage(database), clients).run_update()
    assert result.updated_count == 6
    assert len(database.read_document("exchange_rates")) == 6
    rate_service = RateService(database, clock=lambda: NOW)
    service = TradingService(database, rate_service=rate_service)
    service.register("Алиса", "secret")
    service.login("Алиса", "secret")
    service.deposit(10000)
    result = service.buy("BTC", 0.05)
    assert result.transaction.total == pytest.approx(59337.21 * 0.05)
    assert service.show_portfolio().total == pytest.approx(10000)
    assert rate_service.get_rate("EUR", "USD").rate == pytest.approx(1 / 0.927)
    service.logout()
    restored = TradingService(
        database, rate_service=RateService(database, clock=lambda: NOW)
    )
    restored.login("Алиса", "secret")
    assert restored.show_portfolio().total == pytest.approx(10000)
    assert RateStorage(database).rebuild_cache_from_history().updated_count == 0


def _measurement(pair, rate, seconds):
    timestamp = (
        (NOW + timedelta(seconds=seconds))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    source, target = pair.split("_")
    return {
        "id": f"{pair}_{timestamp}",
        "from_currency": source,
        "to_currency": target,
        "rate": rate,
        "timestamp": timestamp,
        "source": "test",
        "meta": {},
    }


def _save_in_process(directory, record, ready, start, results):
    try:
        settings = SettingsLoader(
            overrides={"data_dir": directory, "lock_timeout_seconds": 10}
        )
        database = DatabaseManager(settings)
        ready.put(True)
        if not start.wait(timeout=15):
            raise RuntimeError("start event timed out")
        result = RateStorage(database).save_measurements([record])
        results.put(("ok", result.added_count))
    except Exception as error:
        results.put(type(error).__name__)


@pytest.mark.parametrize("mode", ["different_pairs", "duplicate", "different_times"])
def test_spawned_updates_preserve_pairs_deduplicate_and_keep_newest(database, mode):
    first = _measurement("BTC_USD", 60000, 0)
    second = {
        "different_pairs": _measurement("EUR_USD", 1.2, 1),
        "duplicate": first,
        "different_times": _measurement("BTC_USD", 61000, 1),
    }[mode]
    context = multiprocessing.get_context("spawn")
    ready, results = context.Queue(), context.Queue()
    start = context.Event()
    processes = [
        context.Process(
            target=_save_in_process,
            args=(str(database.data_dir), record, ready, start, results),
        )
        for record in (first, second)
    ]
    for process in processes:
        process.start()
    try:
        assert ready.get(timeout=15)
        assert ready.get(timeout=15)
        start.set()
        outcomes = [results.get(timeout=15), results.get(timeout=15)]
        assert all(
            isinstance(outcome, tuple) and outcome[0] == "ok" for outcome in outcomes
        ), outcomes
    finally:
        start.set()
        for process in processes:
            process.join(timeout=15)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        ready.close()
        results.close()
    assert all(process.exitcode == 0 for process in processes)
    history = database.read_document("exchange_rates")
    assert len(history) == (1 if mode == "duplicate" else 2)
    assert len({record["id"] for record in history}) == len(history)
    snapshot = database.read_document("rates")
    expected_pairs = (
        {"BTC_USD", "EUR_USD"} if mode == "different_pairs" else {"BTC_USD"}
    )
    assert set(snapshot["pairs"]) == expected_pairs
    assert snapshot["pairs"]["BTC_USD"]["rate"] == (
        61000 if mode == "different_times" else 60000
    )
    assert RateStorage(database).rebuild_cache_from_history().updated_count == 0
