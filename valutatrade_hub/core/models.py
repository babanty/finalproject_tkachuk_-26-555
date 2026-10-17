"""Users, single-currency wallets, and portfolios without storage dependencies."""

from __future__ import annotations

import hashlib
import hmac
import math
import re
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from valutatrade_hub.core.currencies import get_currency
from valutatrade_hub.core.exceptions import InsufficientFundsError, ValidationError
from valutatrade_hub.core.utils import (
    validate_amount,
    validate_number,
    validate_text,
    validate_user_id,
)

MIN_PASSWORD_LENGTH = 4
SALT_BYTES = 16

type RateProvider = Callable[[str, str], float]


@dataclass(frozen=True)
class Transaction:
    """An immutable, validated record of a USD-settled portfolio operation."""

    id: str
    action: str
    currency_code: str
    amount: float
    rate: float
    base: str
    total: float
    timestamp: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.id, str):
            raise ValidationError("Идентификатор операции должен быть UUID")
        try:
            identifier = str(UUID(self.id))
        except ValueError as error:
            raise ValidationError("Идентификатор операции должен быть UUID") from error
        if identifier != self.id:
            raise ValidationError(
                "Идентификатор операции должен быть каноническим UUID"
            )
        if self.action not in ("DEPOSIT", "BUY", "SELL"):
            raise ValidationError("Неизвестный тип операции")
        code = get_currency(self.currency_code).code
        if self.base != "USD":
            raise ValidationError("Расчётная валюта операции должна быть USD")
        amount = validate_amount(self.amount)
        rate = validate_amount(self.rate)
        total = validate_amount(self.total)
        if total != validate_amount(amount * rate):
            raise ValidationError("Стоимость операции не соответствует сумме и курсу")
        if self.action == "DEPOSIT":
            if code != "USD" or rate != 1:
                raise ValidationError("Пополнение допускается только в USD по курсу 1")
        elif code == "USD":
            raise ValidationError("Обмен USD на USD недопустим")
        if (
            not isinstance(self.timestamp, datetime)
            or self.timestamp.utcoffset() is None
        ):
            raise ValidationError("Время операции должно содержать часовой пояс")
        object.__setattr__(self, "currency_code", code)
        object.__setattr__(self, "amount", amount)
        object.__setattr__(self, "rate", rate)
        object.__setattr__(self, "total", total)
        object.__setattr__(self, "timestamp", self.timestamp.astimezone(UTC))


class User:
    """A user with salted password credentials and an immutable identifier."""

    def __init__(
        self,
        user_id: int,
        username: str,
        hashed_password: str,
        salt: str,
        registration_date: datetime,
    ) -> None:
        self._user_id = validate_user_id(user_id)
        self.username = username
        if (
            not isinstance(hashed_password, str)
            or re.fullmatch(r"[0-9a-fA-F]{64}", hashed_password) is None
        ):
            raise ValidationError("Хеш пароля должен быть строкой SHA-256")
        if not isinstance(salt, str) or not salt:
            raise ValidationError("Соль не может быть пустой")
        if not isinstance(registration_date, datetime):
            raise ValidationError("Дата регистрации должна быть datetime")
        if registration_date.utcoffset() is None:
            raise ValidationError("Дата регистрации должна содержать часовой пояс")
        self._hashed_password = hashed_password.lower()
        self._salt = salt
        self._registration_date = registration_date.astimezone(UTC)

    @classmethod
    def create(
        cls,
        user_id: int,
        username: str,
        password: str,
        registration_date: datetime | None = None,
    ) -> User:
        """Create a user from a plain password without retaining that password."""
        cls._validate_password(password)
        salt = secrets.token_hex(SALT_BYTES)
        return cls(
            user_id,
            username,
            cls._hash_password(password, salt),
            salt,
            registration_date if registration_date is not None else datetime.now(UTC),
        )

    @staticmethod
    def _validate_password(password: str) -> None:
        if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
            raise ValidationError("Пароль должен быть не короче 4 символов")

    @staticmethod
    def _hash_password(password: str, salt: str) -> str:
        return hashlib.sha256((password + salt).encode("utf-8")).hexdigest()

    @property
    def user_id(self) -> int:
        """Return the immutable user identifier."""
        return self._user_id

    @property
    def username(self) -> str:
        """Return the normalized, case-sensitive username."""
        return self._username

    @username.setter
    def username(self, value: str) -> None:
        self._username = validate_text(value, "Имя пользователя")

    @property
    def hashed_password(self) -> str:
        """Return the password hash for persistence."""
        return self._hashed_password

    @property
    def salt(self) -> str:
        """Return the password salt for persistence."""
        return self._salt

    @property
    def registration_date(self) -> datetime:
        """Return the registration timestamp in UTC."""
        return self._registration_date

    def get_user_info(self) -> dict[str, int | str]:
        """Return public user information without the hash or salt."""
        return {
            "user_id": self.user_id,
            "username": self.username,
            "registration_date": self.registration_date.isoformat(),
        }

    def change_password(self, new_password: str) -> None:
        """Replace the password hash and salt after successful validation."""
        self._validate_password(new_password)
        salt = secrets.token_hex(SALT_BYTES)
        hashed_password = self._hash_password(new_password, salt)
        self._salt = salt
        self._hashed_password = hashed_password

    def verify_password(self, password: str) -> bool:
        """Compare the supplied password with the saved hash in constant time."""
        if not isinstance(password, str):
            return False
        return hmac.compare_digest(
            self._hash_password(password, self.salt), self.hashed_password
        )


class Wallet:
    """A single-currency balance that cannot become negative or non-finite."""

    def __init__(self, currency_code: str, balance: float = 0.0) -> None:
        self._currency_code = get_currency(currency_code).code
        self.balance = balance

    @property
    def currency_code(self) -> str:
        """Return the immutable normalized currency code."""
        return self._currency_code

    @property
    def balance(self) -> float:
        """Return the wallet balance without display rounding."""
        return self._balance

    @balance.setter
    def balance(self, value: float) -> None:
        self._balance = validate_number(value, field="Баланс", allow_zero=True)

    def deposit(self, amount: float) -> None:
        """Add a positive finite amount, rejecting an overflowing result."""
        validated = validate_amount(amount)
        self.balance = self.balance + validated

    def withdraw(self, amount: float) -> None:
        """Withdraw a positive amount only when the whole amount is available."""
        validated = validate_amount(amount)
        if validated > self.balance:
            raise InsufficientFundsError(self.balance, validated, self.currency_code)
        self.balance = self.balance - validated

    def get_balance_info(self) -> str:
        """Return a display string that also preserves visible tiny balances."""
        return f"{self.currency_code}: {self.balance:.12g}"


class Portfolio:
    """Wallets owned by one user, valued through an injected rate function."""

    def __init__(
        self,
        user_id: int,
        wallets: Mapping[str, Wallet] | None = None,
        *,
        user: User,
        rate_provider: RateProvider,
        transactions: Sequence[Transaction] | None = None,
    ) -> None:
        self._user_id = validate_user_id(user_id)
        if not isinstance(user, User) or user.user_id != self._user_id:
            raise ValidationError("Владелец портфеля не соответствует user_id")
        if not callable(rate_provider):
            raise ValidationError("Источник курсов должен быть вызываемым объектом")
        self._user = user
        self._rate_provider = rate_provider
        self._wallets: dict[str, Wallet] = {}
        self._transactions: list[Transaction] = []
        self._transaction_ids: set[str] = set()
        if transactions is not None:
            if not isinstance(transactions, (list, tuple)):
                raise ValidationError("История операций должна быть списком")
            for transaction in transactions:
                self.add_transaction(transaction)
        if wallets is not None:
            if not isinstance(wallets, Mapping):
                raise ValidationError("Кошельки должны быть словарём")
            for code, wallet in wallets.items():
                normalized = get_currency(code).code
                if not isinstance(wallet, Wallet) or wallet.currency_code != normalized:
                    raise ValidationError("Валюта кошелька не соответствует ключу")
                if normalized in self._wallets:
                    raise ValidationError(f"Кошелёк '{normalized}' указан повторно")
                self._wallets[normalized] = wallet

    @property
    def user_id(self) -> int:
        """Return the immutable owner identifier."""
        return self._user_id

    @property
    def user(self) -> User:
        """Return the portfolio's user without allowing owner replacement."""
        return self._user

    @property
    def wallets(self) -> dict[str, Wallet]:
        """Return a shallow copy of the wallet dictionary."""
        return self._wallets.copy()

    def add_currency(self, currency_code: str) -> Wallet:
        """Return an existing wallet or create an empty one for a known code."""
        code = get_currency(currency_code).code
        if code not in self._wallets:
            self._wallets[code] = Wallet(code)
        return self._wallets[code]

    @property
    def transactions(self) -> tuple[Transaction, ...]:
        """Return immutable history without exposing the internal list."""
        return tuple(self._transactions)

    def add_transaction(self, transaction: Transaction) -> None:
        """Append one validated operation, rejecting duplicate identifiers."""
        if not isinstance(transaction, Transaction):
            raise ValidationError("Некорректная запись операции")
        if transaction.id in self._transaction_ids:
            raise ValidationError("Повторяющийся идентификатор операции")
        self._transactions.append(transaction)
        self._transaction_ids.add(transaction.id)

    def get_wallet(self, currency_code: str) -> Wallet | None:
        """Return the matching wallet, or None for a supported but absent code."""
        return self._wallets.get(get_currency(currency_code).code)

    def get_total_value(self, base_currency: str = "USD") -> float:
        """Value all nonzero balances in the requested currency."""
        return self.get_valuation(base_currency)[1]

    def get_valuation(
        self, base_currency: str = "USD"
    ) -> tuple[dict[str, float], float]:
        """Calculate wallet values and their total in a single pass."""
        base = get_currency(base_currency).code
        values = {}
        for code, wallet in self._wallets.items():
            if wallet.balance == 0:
                values[code] = 0.0
                continue
            if code == base:
                rate = 1.0
            else:
                rate = validate_amount(self._rate_provider(code, base))
            values[code] = validate_number(
                wallet.balance * rate, field="Итог", allow_zero=True
            )
        try:
            total = math.fsum(values.values())
        except OverflowError as error:
            raise ValidationError(
                "Итоговая стоимость портфеля слишком велика"
            ) from error
        return values, validate_number(total, field="Итог", allow_zero=True)
