"""Exercise real inter-process locking with Windows-compatible spawn."""

import multiprocessing

import pytest

from valutatrade_hub.core.exceptions import (
    InsufficientFundsError,
    StorageError,
    ValidationError,
)
from valutatrade_hub.core.usecases import TradingService
from valutatrade_hub.infra.database import DatabaseManager
from valutatrade_hub.infra.settings import SettingsLoader


def _increment_balance(directory, ready, start, results):
    try:
        settings = SettingsLoader(
            overrides={"data_dir": directory, "lock_timeout_seconds": 10}
        )
        repository = DatabaseManager(settings)
        repository.initialize()
        ready.put(True)
        if not start.wait(timeout=15):
            raise RuntimeError("start event timed out")
        for iteration in range(20):
            repository.update_portfolio(
                1,
                lambda portfolio: portfolio.get_wallet("USD").deposit(1),
                rate_provider=lambda from_code, to_code: 1.0,
            )
        results.put("ok")
    except Exception as error:
        results.put(type(error).__name__)


def _try_locked_read(directory, results):
    settings = SettingsLoader(
        overrides={"data_dir": directory, "lock_timeout_seconds": 0.1}
    )
    repository = DatabaseManager(settings)
    try:
        repository.load_users()
    except StorageError as error:
        results.put(str(error))
    else:
        results.put("unexpected success")


def _finish_processes(processes):
    for process in processes:
        process.join(timeout=15)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)


def test_two_processes_do_not_lose_balance_updates(database, saved_account):
    """Forty overlapping read-modify-write operations retain all forty deposits."""
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    results = context.Queue()
    start = context.Event()
    processes = [
        context.Process(
            target=_increment_balance,
            args=(str(database.data_dir), ready, start, results),
        )
        for worker_index in range(2)
    ]
    for process in processes:
        process.start()
    try:
        assert ready.get(timeout=15)
        assert ready.get(timeout=15)
        start.set()
        assert results.get(timeout=15) == "ok"
        assert results.get(timeout=15) == "ok"
    finally:
        start.set()
        _finish_processes(processes)
        ready.close()
        results.close()
    assert all(process.exitcode == 0 for process in processes)
    portfolio = database.get_portfolio(1, rate_provider=lambda from_code, to_code: 1.0)
    assert portfolio.get_wallet("USD").balance == 50


def test_lock_timeout_is_reported_as_domain_error(database):
    """A separate writer cannot enter an already locked repository."""
    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    process = context.Process(
        target=_try_locked_read, args=(str(database.data_dir), results)
    )
    try:
        with database.locked():
            process.start()
            assert "время ожидания блокировки" in results.get(timeout=15)
    finally:
        _finish_processes([process])
        results.close()
    assert process.exitcode == 0


def _register_account(directory, username, ready, start, results):
    settings = SettingsLoader(overrides={"data_dir": directory})
    repository = DatabaseManager(settings)
    service = TradingService(repository, rate_provider=lambda source, target: 1.0)
    ready.put(True)
    if not start.wait(timeout=15):
        raise RuntimeError("start event timed out")
    try:
        user = service.register(username, "secret")
    except ValidationError:
        results.put("duplicate")
    else:
        results.put(user.user_id)


@pytest.mark.parametrize("same_username", [False, True])
def test_concurrent_registration_preserves_unique_ids_and_names(
    database, same_username
):
    """ID allocation and duplicate detection share the account write lock."""
    context = multiprocessing.get_context("spawn")
    ready, results = context.Queue(), context.Queue()
    start = context.Event()
    names = ["Алиса", "Алиса" if same_username else "Боб"]
    processes = [
        context.Process(
            target=_register_account,
            args=(str(database.data_dir), username, ready, start, results),
        )
        for username in names
    ]
    for process in processes:
        process.start()
    try:
        assert ready.get(timeout=15)
        assert ready.get(timeout=15)
        start.set()
        outcomes = {results.get(timeout=15), results.get(timeout=15)}
    finally:
        start.set()
        _finish_processes(processes)
        ready.close()
        results.close()
    assert all(process.exitcode == 0 for process in processes)
    assert outcomes == ({1, "duplicate"} if same_username else {1, 2})
    assert len(database.load_users()) == (1 if same_username else 2)


def _buy_concurrently(directory, ready, start, results):
    try:
        settings = SettingsLoader(overrides={"data_dir": directory})
        repository = DatabaseManager(settings)
        service = TradingService(repository, rate_provider=lambda source, target: 1.0)
        service.login("Алиса", "secret")
        ready.put(True)
        if not start.wait(timeout=15):
            raise RuntimeError("start event timed out")
        succeeded, rejected = 0, 0
        for iteration in range(10):
            try:
                service.buy("BTC", 1)
            except InsufficientFundsError:
                rejected += 1
            else:
                succeeded += 1
        results.put((succeeded, rejected))
    except Exception as error:
        results.put(type(error).__name__)


def test_concurrent_purchases_cannot_overspend_or_lose_history(database, saved_account):
    """Twenty concurrent attempts spend ten USD exactly once and retain ten buys."""
    context = multiprocessing.get_context("spawn")
    ready, results = context.Queue(), context.Queue()
    start = context.Event()
    processes = [
        context.Process(
            target=_buy_concurrently,
            args=(str(database.data_dir), ready, start, results),
        )
        for worker_index in range(2)
    ]
    for process in processes:
        process.start()
    try:
        assert ready.get(timeout=15)
        assert ready.get(timeout=15)
        start.set()
        outcomes = [results.get(timeout=15), results.get(timeout=15)]
        assert all(isinstance(outcome, tuple) for outcome in outcomes), outcomes
        assert tuple(map(sum, zip(*outcomes))) == (10, 10)
    finally:
        start.set()
        _finish_processes(processes)
        ready.close()
        results.close()
    assert all(process.exitcode == 0 for process in processes)
    saved = database.get_portfolio(1, rate_provider=lambda source, target: 1.0)
    assert saved.get_wallet("USD").balance == 0
    assert saved.get_wallet("BTC").balance == 10
    assert saved.get_total_value() == 10
    assert len(saved.transactions) == 10
    assert len({item.id for item in saved.transactions}) == 10
