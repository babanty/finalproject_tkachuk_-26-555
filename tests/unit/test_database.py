"""Repository serialization, validation, and exception-safe mutations."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from valutatrade_hub.core.exceptions import (
    InsufficientFundsError,
    StorageError,
    ValidationError,
)
from valutatrade_hub.core.models import Portfolio, User, Wallet
from valutatrade_hub.infra import database as database_module
from valutatrade_hub.infra.database import DOCUMENT_FILES, DatabaseManager
from valutatrade_hub.infra.json_io import atomic_write_json, read_json
from valutatrade_hub.infra.serialization import user_to_dict
from valutatrade_hub.infra.settings import SettingsLoader


def no_rates(from_code, to_code):
    """Fail if persistence unexpectedly tries to value a portfolio."""
    raise AssertionError("persistence must not fetch rates")


def test_constructing_singletons_does_not_initialize_files(tmp_path):
    """Creating dependencies does not implicitly create runtime state."""
    settings = SettingsLoader(overrides={"data_dir": tmp_path / "data"})
    repository = DatabaseManager(settings)
    assert DatabaseManager() is repository
    assert DatabaseManager(settings) is repository
    assert not repository.data_dir.exists()
    with pytest.raises(StorageError, match="initialize"):
        repository.load_users()


def test_database_rejects_another_explicit_root(database, tmp_path, monkeypatch):
    """Repeated construction cannot redirect an existing repository silently."""
    monkeypatch.setattr(SettingsLoader, "_instance", None)
    other = SettingsLoader(overrides={"data_dir": tmp_path / "other"})
    with pytest.raises(ValidationError, match="другого хранилища"):
        DatabaseManager(other)
    assert database.data_dir.name == "кошельки с пробелами"


def test_initialization_creates_expected_empty_documents(database):
    """Explicit initialization materializes all four canonical JSON files."""
    assert {path.name for path in database.data_dir.glob("*.json")} == set(
        DOCUMENT_FILES.values()
    )
    assert database.load_users() == []
    assert database.read_document("portfolios") == []
    assert database.read_document("rates") == {"pairs": {}, "last_refresh": None}
    assert database.read_document("exchange_rates") == []


def test_reinitialization_preserves_existing_account(database, saved_account):
    """initialize() validates rather than overwriting previously saved files."""
    before = {name: database.get_path(name).read_bytes() for name in DOCUMENT_FILES}
    database.initialize()
    assert {
        name: database.get_path(name).read_bytes() for name in DOCUMENT_FILES
    } == before


def test_invalid_existing_file_is_not_reset_or_followed_by_other_creations(tmp_path):
    """Validate all existing documents before creating any missing JSON."""
    directory = tmp_path / "data"
    directory.mkdir()
    path = directory / "rates.json"
    path.write_text("{bad", encoding="utf-8")
    repository = DatabaseManager(SettingsLoader(overrides={"data_dir": directory}))
    with pytest.raises(StorageError):
        repository.initialize()
    assert [item.name for item in directory.glob("*.json")] == ["rates.json"]
    assert path.read_text(encoding="utf-8") == "{bad"


def test_user_and_multicurrency_portfolio_round_trip(database):
    """Hashes, salts, ISO dates, balances, and the owner relation survive storage."""
    user = User.create(1, "Алиса", "пароль", datetime(2026, 10, 17, tzinfo=UTC))
    portfolio = Portfolio(
        1,
        {"USD": Wallet("USD", 100), "BTC": Wallet("BTC", 0.00000001)},
        user=user,
        rate_provider=no_rates,
    )
    database.save_account(user, portfolio)
    restored = database.find_user(" Алиса ")
    assert restored is not user
    assert restored.verify_password("пароль")
    assert restored.salt == user.salt
    assert restored.hashed_password == user.hashed_password
    assert restored.registration_date == user.registration_date
    saved = database.read_document("users")[0]
    assert saved["registration_date"] == "2026-10-17T00:00:00Z"
    assert "пароль" not in database.get_path("users").read_text(encoding="utf-8")
    loaded = database.get_portfolio(1, rate_provider=no_rates)
    assert loaded.user.username == "Алиса"
    assert loaded.get_wallet("BTC").balance == 0.00000001
    assert loaded.get_wallet("USD").balance == 100
    assert database.get_user(999) is None
    assert database.find_user("unknown") is None
    assert database.get_portfolio(999, rate_provider=no_rates) is None


@pytest.mark.parametrize("document", ["users", "portfolios"])
def test_wrong_document_schema_is_an_error(database, document):
    """An object cannot be accepted where a list of accounts is required."""
    path = database.get_path(document)
    atomic_write_json(path, {})
    with pytest.raises(StorageError):
        database.load_users()
    assert read_json(path) == {}


@pytest.mark.parametrize(
    "wallet",
    [
        {"balance": -1},
        {"balance": True},
        {"balance": "1"},
        {"balance": 1, "currency_code": "EUR"},
        {},
    ],
)
def test_bad_wallet_is_not_treated_as_empty(database, saved_account, wallet):
    """Malformed saved wallet fields do not disappear during deserialization."""
    path = database.get_path("portfolios")
    invalid = [{"user_id": 1, "wallets": {"USD": wallet}}]
    atomic_write_json(path, invalid)
    with pytest.raises(StorageError):
        database.get_portfolio(1, rate_provider=no_rates)
    assert read_json(path) == invalid


def test_dangling_account_is_detected_without_fabricating_portfolio(database):
    """A missing portfolio needs explicit recovery rather than an empty reset."""
    atomic_write_json(
        database.get_path("users"), [user_to_dict(User.create(1, "a", "1234"))]
    )
    with pytest.raises(StorageError, match="восстановление"):
        database.initialize()
    assert read_json(database.get_path("portfolios")) == []


def test_duplicate_username_is_rejected_before_writes(database, saved_account):
    """Uniqueness validation applies to the whole prospective snapshot."""
    user = User.create(2, "Алиса", "1234")
    portfolio = Portfolio(2, user=user, rate_provider=no_rates)
    with pytest.raises(StorageError, match="Повторяющийся"):
        database.save_account(user, portfolio)
    assert len(database.load_users()) == 1


def test_failed_second_account_write_rolls_back_first(database, monkeypatch):
    """Ordinary portfolio-write failures restore the previous users document."""
    user = User.create(1, "alice", "1234")
    portfolio = Portfolio(1, user=user, rate_provider=no_rates)
    real_write = database_module.atomic_write_json

    def fail_portfolio(path, data):
        if path.name == "portfolios.json":
            raise StorageError("simulated second-write failure")
        real_write(path, data)

    monkeypatch.setattr(database_module, "atomic_write_json", fail_portfolio)
    with pytest.raises(StorageError, match="восстановлены"):
        database.save_account(user, portfolio)
    assert database.load_users() == []
    assert database.read_document("portfolios") == []


def test_failed_rollback_reports_recovery_requirement(database, monkeypatch):
    """A failed compensation must not be described as a successful rollback."""
    user = User.create(1, "alice", "1234")
    real_write = database_module.atomic_write_json
    calls = []

    def fail_after_first_write(path, data):
        calls.append(path.name)
        if len(calls) > 1:
            raise StorageError("simulated write failure")
        real_write(path, data)

    monkeypatch.setattr(database_module, "atomic_write_json", fail_after_first_write)
    with pytest.raises(StorageError, match="требуется восстановление"):
        database.save_account(user, Portfolio(1, user=user, rate_provider=no_rates))
    assert calls == ["users.json", "portfolios.json", "users.json"]
    with pytest.raises(StorageError, match="восстановление"):
        database.initialize()


def test_mutation_is_saved_from_fresh_state_and_returned_objects_are_detached(
    database, saved_account
):
    """Changing a returned model later cannot change a saved JSON balance."""
    updated = database.update_portfolio(
        1,
        lambda portfolio: portfolio.get_wallet("USD").deposit(5),
        rate_provider=no_rates,
    )
    assert updated.get_wallet("USD").balance == 15
    updated.get_wallet("USD").deposit(100)
    assert (
        database.get_portfolio(1, rate_provider=no_rates).get_wallet("USD").balance
        == 15
    )


def test_mutation_failure_preserves_both_wallets(database, saved_account):
    """A callback's earlier changes remain local when a later validation fails."""
    before = database.get_path("portfolios").read_bytes()

    def change_then_fail(portfolio):
        portfolio.add_currency("BTC").deposit(0.5)
        portfolio.get_wallet("USD").withdraw(100)

    with pytest.raises(InsufficientFundsError):
        database.update_portfolio(1, change_then_fail, rate_provider=no_rates)
    assert database.get_path("portfolios").read_bytes() == before


def test_update_preserves_extra_transaction_metadata(database, saved_account):
    """Wallet serialization does not discard a portfolio's transaction list."""
    records = database.read_document("portfolios")
    records[0]["transactions"] = [
        {
            "id": "40d2d056-49f2-4fb7-bc93-999071d28131",
            "action": "DEPOSIT",
            "currency_code": "USD",
            "amount": 10,
            "rate": 1,
            "base": "USD",
            "total": 10,
            "timestamp": "2026-10-17T00:00:00Z",
        }
    ]
    atomic_write_json(database.get_path("portfolios"), records)
    database.update_portfolio(
        1,
        lambda portfolio: portfolio.get_wallet("USD").deposit(1),
        rate_provider=no_rates,
    )
    assert (
        database.read_document("portfolios")[0]["transactions"]
        == records[0]["transactions"]
    )


def test_replace_failure_does_not_persist_mutation(
    database, saved_account, monkeypatch
):
    """Repository mutations retain their old file if the final replacement fails."""
    from valutatrade_hub.infra import json_io

    before = database.get_path("portfolios").read_bytes()

    def fail_replace(source, destination):
        raise PermissionError("simulated failure")

    monkeypatch.setattr(json_io.os, "replace", fail_replace)
    with pytest.raises(StorageError):
        database.update_portfolio(
            1,
            lambda portfolio: portfolio.get_wallet("USD").deposit(1),
            rate_provider=no_rates,
        )
    assert database.get_path("portfolios").read_bytes() == before


def test_reentrant_lock_allows_account_creation_from_usecase_transaction(database):
    """A future registration usecase can reserve an ID and save under one lock."""
    with database.locked():
        user = User.create(len(database.load_users()) + 1, "alice", "1234")
        database.save_account(user, Portfolio(1, user=user, rate_provider=no_rates))
    assert database.get_user(1).username == "alice"


def test_arbitrary_document_paths_are_rejected(database):
    """Repository paths are restricted to canonical data documents."""
    with pytest.raises(StorageError, match="Неизвестный документ"):
        database.read_document("../.env")


def test_concurrent_construction_keeps_one_repository(tmp_path):
    """One repository and lock are shared even during concurrent initialization."""
    settings = SettingsLoader(overrides={"data_dir": tmp_path / "data"})
    with ThreadPoolExecutor(max_workers=8) as executor:
        instances = list(
            executor.map(lambda index: DatabaseManager(settings), range(24))
        )
    assert all(instance is instances[0] for instance in instances)
    assert not instances[0].data_dir.exists()
