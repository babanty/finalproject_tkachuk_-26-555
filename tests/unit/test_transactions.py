"""Persisted trade history is validated, immutable, and never silently dropped."""

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime

import pytest

from valutatrade_hub.core.exceptions import StorageError, ValidationError
from valutatrade_hub.core.models import Portfolio, Transaction
from valutatrade_hub.infra.json_io import atomic_write_json
from valutatrade_hub.infra.serialization import transaction_to_dict


@pytest.fixture
def transaction():
    return Transaction(
        "40d2d056-49f2-4fb7-bc93-999071d28131",
        "BUY",
        "BTC",
        0.05,
        60000,
        "USD",
        3000,
        datetime(2026, 10, 17, tzinfo=UTC),
    )


def test_history_round_trip_and_immutable_public_view(
    database, saved_account, transaction
):
    updated = database.update_portfolio(
        1,
        lambda current: current.add_transaction(transaction),
        rate_provider=lambda source, target: 1,
    )
    restored = database.get_portfolio(1, rate_provider=lambda source, target: 1)
    assert restored.transactions == (transaction,)
    assert isinstance(restored.transactions, tuple)
    with pytest.raises(FrozenInstanceError):
        restored.transactions[0].amount = 100
    updated.add_transaction(
        replace(transaction, id="74a5063b-4b5c-4ec1-8661-2af35cf6e30c")
    )
    assert len(database.read_document("portfolios")[0]["transactions"]) == 1
    assert database.read_document("portfolios")[0]["transactions"][0][
        "timestamp"
    ].endswith("Z")


def test_duplicate_transaction_rolls_back_other_mutations(
    database, saved_account, transaction
):
    database.update_portfolio(
        1,
        lambda current: current.add_transaction(transaction),
        rate_provider=lambda source, target: 1,
    )
    before = database.get_path("portfolios").read_bytes()

    def mutate(current):
        current.get_wallet("USD").deposit(100)
        current.add_transaction(transaction)

    with pytest.raises(ValidationError, match="Повторяющийся"):
        database.update_portfolio(1, mutate, rate_provider=lambda source, target: 1)
    assert database.get_path("portfolios").read_bytes() == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "bad"),
        ("id", None),
        ("action", "TRANSFER"),
        ("currency_code", "ABC"),
        ("currency_code", "USD"),
        ("amount", -1),
        ("amount", True),
        ("rate", 0),
        ("rate", "60000"),
        ("total", 1),
        ("base", "EUR"),
        ("timestamp", datetime(2026, 10, 17)),
    ],
)
def test_invalid_history_is_reported_without_replacing_file(
    database, saved_account, transaction, field, value
):
    records = database.read_document("portfolios")
    record = transaction_to_dict(transaction)
    record[field] = value.isoformat() if isinstance(value, datetime) else value
    records[0]["transactions"] = [record]
    atomic_write_json(database.get_path("portfolios"), records)
    before = database.get_path("portfolios").read_bytes()
    with pytest.raises(StorageError):
        database.initialize()
    assert database.get_path("portfolios").read_bytes() == before


def test_duplicate_persisted_ids_are_rejected(database, saved_account, transaction):
    records = database.read_document("portfolios")
    records[0]["transactions"] = [transaction_to_dict(transaction)] * 2
    atomic_write_json(database.get_path("portfolios"), records)
    with pytest.raises(StorageError):
        database.get_portfolio(1, rate_provider=lambda source, target: 1)


@pytest.mark.parametrize(("code", "rate", "total"), [("BTC", 1, 10), ("USD", 2, 20)])
def test_deposit_history_requires_usd_at_rate_one(transaction, code, rate, total):
    with pytest.raises(ValidationError):
        replace(
            transaction,
            action="DEPOSIT",
            currency_code=code,
            amount=10,
            rate=rate,
            total=total,
        )


def test_legacy_portfolio_without_history_remains_readable(database, saved_account):
    records = database.read_document("portfolios")
    del records[0]["transactions"]
    atomic_write_json(database.get_path("portfolios"), records)
    assert (
        database.get_portfolio(1, rate_provider=lambda source, target: 1).transactions
        == ()
    )


def test_saving_account_does_not_erase_existing_history(
    database, saved_account, transaction
):
    user, old_portfolio = saved_account
    database.update_portfolio(
        1,
        lambda current: current.add_transaction(transaction),
        rate_provider=lambda source, target: 1,
    )
    database.save_account(user, old_portfolio)
    assert database.get_portfolio(
        1, rate_provider=lambda source, target: 1
    ).transactions == (transaction,)


def test_constructor_rejects_invalid_history_elements(user, rate_provider):
    with pytest.raises(ValidationError):
        Portfolio(1, user=user, rate_provider=rate_provider, transactions=[{}])
