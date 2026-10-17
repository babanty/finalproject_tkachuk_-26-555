"""Model serialization and validation of persisted JSON documents."""

from datetime import UTC, datetime
from typing import Any

from valutatrade_hub.core.currencies import get_currency
from valutatrade_hub.core.exceptions import (
    ApiRequestError,
    StorageError,
    ValutaTradeError,
)
from valutatrade_hub.core.models import Portfolio, RateProvider, User, Wallet
from valutatrade_hub.core.utils import validate_amount, validate_text


def _unconfigured_rate(from_code: str, to_code: str) -> float:
    raise ApiRequestError("Источник курсов не настроен")


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Expected an ISO timestamp")
    timestamp = datetime.fromisoformat(value)
    if timestamp.utcoffset() is None:
        raise ValueError("Timestamp requires a timezone")
    return timestamp.astimezone(UTC)


def user_to_dict(user: User) -> dict:
    """Serialize the full credentials needed to restore a user."""
    return {
        "user_id": user.user_id,
        "username": user.username,
        "hashed_password": user.hashed_password,
        "salt": user.salt,
        "registration_date": user.registration_date.isoformat().replace("+00:00", "Z"),
    }


def user_from_dict(record: Any) -> User:
    """Restore a user while rejecting malformed persisted fields."""
    try:
        if not isinstance(record, dict):
            raise ValueError("Expected an object")
        return User(
            record["user_id"],
            record["username"],
            record["hashed_password"],
            record["salt"],
            _timestamp(record["registration_date"]),
        )
    except (KeyError, TypeError, ValueError, ValutaTradeError) as error:
        raise StorageError("Некорректная запись пользователя в users.json") from error


def portfolio_to_dict(portfolio: Portfolio) -> dict:
    """Serialize a portfolio's ownership and wallet balances."""
    return {
        "user_id": portfolio.user_id,
        "wallets": {
            code: {"currency_code": code, "balance": wallet.balance}
            for code, wallet in portfolio.wallets.items()
        },
    }


def portfolio_from_dict(
    record: Any, *, user: User, rate_provider: RateProvider
) -> Portfolio:
    """Restore wallets without fetching rates or discarding invalid balances."""
    try:
        if not isinstance(record, dict) or not isinstance(record["wallets"], dict):
            raise ValueError("Expected a portfolio and wallet objects")
        wallets = {}
        for code, saved_wallet in record["wallets"].items():
            if not isinstance(saved_wallet, dict):
                raise ValueError("Expected a wallet object")
            if saved_wallet.get("currency_code", code) != code:
                raise ValueError("Wallet code does not match its key")
            wallets[code] = Wallet(code, saved_wallet["balance"])
        if not isinstance(record.get("transactions", []), list):
            raise ValueError("Expected a transaction list")
        return Portfolio(
            record["user_id"], wallets, user=user, rate_provider=rate_provider
        )
    except (KeyError, TypeError, ValueError, ValutaTradeError) as error:
        raise StorageError("Некорректная запись портфеля в portfolios.json") from error


def validate_accounts(users: Any, portfolios: Any) -> None:
    """Validate both account documents and their one-to-one owner relation."""
    if not isinstance(users, list) or not isinstance(portfolios, list):
        raise StorageError("users.json и portfolios.json должны содержать списки")
    owners = {}
    usernames = set()
    for record in users:
        user = user_from_dict(record)
        if user.user_id in owners or user.username in usernames:
            raise StorageError("Повторяющийся идентификатор или имя пользователя")
        owners[user.user_id] = user
        usernames.add(user.username)
    portfolio_ids = set()
    for record in portfolios:
        if not isinstance(record, dict):
            raise StorageError("Некорректная запись портфеля")
        user_id = record.get("user_id")
        if isinstance(user_id, bool) or not isinstance(user_id, int):
            raise StorageError("Некорректный владелец портфеля")
        if user_id not in owners or user_id in portfolio_ids:
            raise StorageError("Нарушена связь пользователя и портфеля")
        portfolio_from_dict(
            record, user=owners[user_id], rate_provider=_unconfigured_rate
        )
        portfolio_ids.add(user_id)
    if portfolio_ids != owners.keys():
        raise StorageError(
            "Для пользователя отсутствует портфель; требуется восстановление"
        )


def _validate_quote(record: Any, *, history: bool) -> None:
    if not isinstance(record, dict):
        raise ValueError("Expected a quote object")
    validate_amount(record["rate"])
    _timestamp(record["timestamp" if history else "updated_at"])
    validate_text(record["source"], "Источник")


def _validate_pair(pair: str) -> None:
    if not isinstance(pair, str):
        raise ValueError("Expected a string pair")
    from_code, to_code = pair.split("_")
    if (
        get_currency(from_code).code != from_code
        or to_code != "USD"
        or from_code == "USD"
    ):
        raise ValueError("Expected an uppercase currency-to-USD pair")


def validate_rate_document(data: Any, *, history: bool = False) -> None:
    """Validate rate snapshots or history before they reach storage consumers."""
    try:
        if history:
            if not isinstance(data, list):
                raise ValueError("Expected a history list")
            identifiers = set()
            for record in data:
                _validate_quote(record, history=True)
                pair = f"{record['from_currency']}_{record['to_currency']}"
                _validate_pair(pair)
                expected_id = f"{pair}_{record['timestamp']}"
                if record["id"] != expected_id or record["id"] in identifiers:
                    raise ValueError("Invalid or duplicate history identifier")
                if not isinstance(record.get("meta", {}), dict):
                    raise ValueError("Expected metadata object")
                identifiers.add(record["id"])
        else:
            if not isinstance(data, dict) or not isinstance(data["pairs"], dict):
                raise ValueError("Expected a rate snapshot")
            if data["last_refresh"] is not None:
                _timestamp(data["last_refresh"])
            elif data["pairs"]:
                raise ValueError("A nonempty snapshot requires last_refresh")
            for pair, record in data["pairs"].items():
                _validate_pair(pair)
                _validate_quote(record, history=False)
    except (KeyError, TypeError, ValueError, ValutaTradeError) as error:
        raise StorageError("Некорректный формат истории или снимка курсов") from error
