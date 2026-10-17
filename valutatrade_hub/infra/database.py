"""One JSON repository per process, with a shared inter-process lock."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Any, ClassVar

from filelock import FileLock, Timeout

from valutatrade_hub.core.exceptions import StorageError, ValidationError
from valutatrade_hub.core.models import Portfolio, RateProvider, User
from valutatrade_hub.core.utils import validate_text, validate_user_id
from valutatrade_hub.infra.json_io import atomic_write_json, read_json
from valutatrade_hub.infra.serialization import (
    portfolio_from_dict,
    portfolio_to_dict,
    user_from_dict,
    user_to_dict,
    validate_accounts,
    validate_rate_document,
)
from valutatrade_hub.infra.settings import SettingsLoader

DOCUMENT_FILES = {
    "users": "users.json",
    "portfolios": "portfolios.json",
    "rates": "rates.json",
    "exchange_rates": "exchange_rates.json",
}


class DatabaseManager:
    """Use __new__ for one repository and one reusable lock per process."""

    _instance: ClassVar[DatabaseManager | None] = None
    _creation_lock = RLock()

    def __new__(cls, settings=None):
        with cls._creation_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
            return cls._instance

    def __init__(self, settings: SettingsLoader | None = None) -> None:
        with self._creation_lock:
            self._configure(settings)

    def _configure(self, settings: SettingsLoader | None) -> None:
        if getattr(self, "_initialized", False):
            if settings is not None and (
                settings.get("data_dir") != self.data_dir
                or settings.get("lock_timeout_seconds") != self._lock.timeout
            ):
                raise ValidationError(
                    "DatabaseManager уже настроен для другого хранилища"
                )
            return
        self._settings = settings if settings is not None else SettingsLoader()
        self._data_dir = self._settings.get("data_dir")
        self._lock = FileLock(
            self._data_dir / ".store.lock",
            timeout=self._settings.get("lock_timeout_seconds"),
            preserve_lock_file=True,
        )
        self._initialized = True

    @property
    def data_dir(self) -> Path:
        """Return the absolute location of this repository's data."""
        return self._data_dir

    def get_path(self, document: str) -> Path:
        """Resolve a known document name without accepting arbitrary paths."""
        if document not in DOCUMENT_FILES:
            raise StorageError("Неизвестный документ хранилища")
        return self.data_dir / DOCUMENT_FILES[document]

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Serialize read-modify-write operations across threads and processes."""
        if not self.data_dir.is_dir():
            raise StorageError("Хранилище не инициализировано: выполните initialize()")
        try:
            self._lock.acquire()
        except Timeout as error:
            raise StorageError(
                "Хранилище занято: истекло время ожидания блокировки"
            ) from error
        except OSError as error:
            raise StorageError("Не удалось заблокировать хранилище") from error
        try:
            yield
        finally:
            self._lock.release()

    def initialize(self) -> None:
        """Validate existing files first, then create only missing empty files."""
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise StorageError("Не удалось создать каталог данных") from error
        defaults = {
            "users": [],
            "portfolios": [],
            "rates": {"pairs": {}, "last_refresh": None},
            "exchange_rates": [],
        }
        with self.locked():
            documents = {}
            missing = []
            for name, default in defaults.items():
                path = self.get_path(name)
                if path.exists():
                    documents[name] = read_json(path)
                else:
                    documents[name] = default
                    missing.append(name)
            validate_accounts(documents["users"], documents["portfolios"])
            validate_rate_document(documents["rates"])
            validate_rate_document(documents["exchange_rates"], history=True)
            for name in missing:
                atomic_write_json(self.get_path(name), documents[name])

    def _read_accounts(self) -> tuple[list[dict], list[dict]]:
        users = read_json(self.get_path("users"))
        portfolios = read_json(self.get_path("portfolios"))
        validate_accounts(users, portfolios)
        return users, portfolios

    def read_document(self, document: str) -> Any:
        """Read a validated independent JSON document under the shared lock."""
        path = self.get_path(document)
        with self.locked():
            if document in {"users", "portfolios"}:
                users, portfolios = self._read_accounts()
                return users if document == "users" else portfolios
            data = read_json(path)
            validate_rate_document(data, history=document == "exchange_rates")
            return data

    def load_users(self) -> list[User]:
        """Return restored users with their salted credentials."""
        return [user_from_dict(record) for record in self.read_document("users")]

    def get_user(self, user_id: int) -> User | None:
        """Find a user by identifier or return None if no such user exists."""
        validate_user_id(user_id)
        return next(
            (user for user in self.load_users() if user.user_id == user_id), None
        )

    def find_user(self, username: str) -> User | None:
        """Find a trimmed, case-sensitive username."""
        username = validate_text(username, "Имя пользователя")
        return next(
            (user for user in self.load_users() if user.username == username), None
        )

    def get_portfolio(
        self, user_id: int, *, rate_provider: RateProvider
    ) -> Portfolio | None:
        """Load a portfolio and its owner from a single locked snapshot."""
        validate_user_id(user_id)
        with self.locked():
            users, portfolios = self._read_accounts()
            owner = next((item for item in users if item["user_id"] == user_id), None)
            if owner is None:
                return None
            record = next(item for item in portfolios if item["user_id"] == user_id)
            return portfolio_from_dict(
                record, user=user_from_dict(owner), rate_provider=rate_provider
            )

    def save_account(self, user: User, portfolio: Portfolio) -> None:
        """Save an account pair, compensating an ordinary second-write failure."""
        if user.user_id != portfolio.user_id:
            raise ValidationError("Владелец портфеля не соответствует пользователю")
        with self.locked():
            users, portfolios = self._read_accounts()
            previous_portfolio = next(
                (item for item in portfolios if item["user_id"] == user.user_id), {}
            )
            saved_portfolio = {**previous_portfolio, **portfolio_to_dict(portfolio)}
            updated_users = [item for item in users if item["user_id"] != user.user_id]
            updated_users.append(user_to_dict(user))
            updated_portfolios = [
                item for item in portfolios if item["user_id"] != user.user_id
            ]
            updated_portfolios.append(saved_portfolio)
            validate_accounts(updated_users, updated_portfolios)
            atomic_write_json(self.get_path("users"), updated_users)
            try:
                atomic_write_json(self.get_path("portfolios"), updated_portfolios)
            except StorageError as error:
                try:
                    atomic_write_json(self.get_path("users"), users)
                except StorageError as rollback_error:
                    raise StorageError(
                        "Сохранение и откат не удались: требуется восстановление данных"
                    ) from rollback_error
                raise StorageError(
                    "Аккаунт не сохранён; предыдущие данные восстановлены"
                ) from error

    def update_portfolio(
        self,
        user_id: int,
        mutation: Callable[[Portfolio], None],
        *,
        rate_provider: RateProvider,
    ) -> Portfolio:
        """Apply a mutation to freshly loaded wallets and save only on success."""
        validate_user_id(user_id)
        with self.locked():
            users, portfolios = self._read_accounts()
            owner = next((item for item in users if item["user_id"] == user_id), None)
            if owner is None:
                raise StorageError("Пользователь для изменения портфеля не найден")
            record = next(item for item in portfolios if item["user_id"] == user_id)
            portfolio = portfolio_from_dict(
                record, user=user_from_dict(owner), rate_provider=rate_provider
            )
            mutation(portfolio)
            record.update(portfolio_to_dict(portfolio))
            validate_accounts(users, portfolios)
            atomic_write_json(self.get_path("portfolios"), portfolios)
            return portfolio
