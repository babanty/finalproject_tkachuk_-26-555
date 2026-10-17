"""Application operations with an in-memory session and injected storage."""

from dataclasses import dataclass

from valutatrade_hub.core.currencies import get_currency
from valutatrade_hub.core.exceptions import (
    AuthenticationError,
    StorageError,
    ValidationError,
)
from valutatrade_hub.core.models import Portfolio, RateProvider, User
from valutatrade_hub.core.utils import validate_text
from valutatrade_hub.infra.database import DatabaseManager


@dataclass(frozen=True)
class UserInfo:
    """Public account details returned without credentials."""

    user_id: int
    username: str


@dataclass(frozen=True)
class PortfolioView:
    """A detached portfolio summary for presentation."""

    username: str
    base_currency: str
    balances: tuple[tuple[str, float], ...]
    total: float


class TradingService:
    """Coordinate account operations; keep authentication within this instance."""

    def __init__(
        self, database: DatabaseManager, *, rate_provider: RateProvider
    ) -> None:
        self._database = database
        self._rate_provider = rate_provider
        self._current_user: UserInfo | None = None

    @property
    def current_user(self) -> UserInfo | None:
        """Return public session details, or None before successful login."""
        return self._current_user

    def register(self, username: str, password: str) -> UserInfo:
        """Persist a unique account and empty portfolio under one shared lock."""
        username = validate_text(username, "Имя пользователя")
        with self._database.locked():
            users = self._database.load_users()
            if any(user.username == username for user in users):
                raise ValidationError(f"Имя пользователя '{username}' уже занято")
            user_id = max((user.user_id for user in users), default=0) + 1
            user = User.create(user_id, username, password)
            portfolio = Portfolio(user_id, user=user, rate_provider=self._rate_provider)
            self._database.save_account(user, portfolio)
        return UserInfo(user.user_id, user.username)

    def login(self, username: str, password: str) -> UserInfo:
        """Replace the session only after successful credential verification."""
        self.logout()
        username = validate_text(username, "Имя пользователя")
        user = self._database.find_user(username)
        if user is None:
            raise AuthenticationError(f"Пользователь '{username}' не найден")
        if not user.verify_password(password):
            raise AuthenticationError("Неверный пароль")
        self._current_user = UserInfo(user.user_id, user.username)
        return self._current_user

    def logout(self) -> None:
        """Discard any current session without modifying stored accounts."""
        self._current_user = None

    def show_portfolio(self, base_currency: str = "USD") -> PortfolioView:
        """Read the signed-in user's current balances and total value."""
        if self._current_user is None:
            raise AuthenticationError("Сначала выполните login")
        base = get_currency(base_currency).code
        portfolio = self._database.get_portfolio(
            self._current_user.user_id, rate_provider=self._rate_provider
        )
        if portfolio is None:
            self.logout()
            raise StorageError("Портфель текущего пользователя не найден")
        return PortfolioView(
            username=portfolio.user.username,
            base_currency=base,
            balances=tuple(
                (code, wallet.balance)
                for code, wallet in sorted(portfolio.wallets.items())
            ),
            total=portfolio.get_total_value(base),
        )
