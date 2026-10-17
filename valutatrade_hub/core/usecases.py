"""Application operations with an in-memory session and injected storage."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from valutatrade_hub.core.currencies import get_currency
from valutatrade_hub.core.exceptions import (
    AuthenticationError,
    InsufficientFundsError,
    StorageError,
    ValidationError,
)
from valutatrade_hub.core.models import Portfolio, RateProvider, Transaction, User
from valutatrade_hub.core.rates import RateQuote, RateService
from valutatrade_hub.core.utils import validate_amount, validate_number, validate_text
from valutatrade_hub.decorators import log_action, set_action_quote
from valutatrade_hub.infra.database import DatabaseManager


@dataclass(frozen=True)
class UserInfo:
    """Public account details returned without credentials."""

    user_id: int
    username: str


@dataclass(frozen=True)
class BalanceChange:
    """One persisted wallet change, ready for display or action logging."""

    currency_code: str
    before: float
    after: float


@dataclass(frozen=True)
class OperationResult:
    """Public details returned only after the entire operation is saved."""

    user_id: int
    username: str
    transaction: Transaction
    changes: tuple[BalanceChange, ...]


@dataclass(frozen=True)
class WalletValuation:
    """A balance and its value in the portfolio's display currency."""

    currency_code: str
    balance: float
    value: float


@dataclass(frozen=True)
class PortfolioView:
    """A detached portfolio summary for presentation."""

    username: str
    base_currency: str
    wallets: tuple[WalletValuation, ...]
    total: float

    @property
    def balances(self) -> tuple[tuple[str, float], ...]:
        """Return currency balances independently of their display valuation."""
        return tuple((wallet.currency_code, wallet.balance) for wallet in self.wallets)


class TradingService:
    """Coordinate account operations; keep authentication within this instance."""

    def __init__(
        self,
        database: DatabaseManager,
        *,
        rate_provider: RateProvider | None = None,
        rate_service: RateService | None = None,
    ) -> None:
        self._database = database
        if (rate_provider is None) == (rate_service is None):
            raise ValidationError("Укажите один источник курсов")
        self._rates = rate_service
        self._rate_provider = (
            rate_provider
            if rate_provider is not None
            else lambda source, target: self.get_rate(source, target).rate
        )
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
        quotes = {}
        portfolio = self._database.get_portfolio(
            self._current_user.user_id,
            rate_provider=(
                self._rate_provider
                if self._rates is None
                else lambda source, target: quotes[(source, target)].rate
            ),
        )
        if portfolio is None:
            self.logout()
            raise StorageError("Портфель текущего пользователя не найден")
        if self._rates is not None:
            quotes.update(
                self._rates.get_rates(
                    (code, base)
                    for code, wallet in portfolio.wallets.items()
                    if wallet.balance != 0 and code != base
                )
            )
        values, total = portfolio.get_valuation(base)
        return PortfolioView(
            username=portfolio.user.username,
            base_currency=base,
            wallets=tuple(
                WalletValuation(code, wallet.balance, values[code])
                for code, wallet in sorted(portfolio.wallets.items())
            ),
            total=total,
        )

    def get_rate(self, from_code: str, to_code: str) -> RateQuote:
        """Return a market quote independently of the authentication session."""
        if self._rates is None:
            raise ValidationError("Сервис курсов не настроен")
        return self._rates.get_rate(from_code, to_code)

    def _require_user(self) -> UserInfo:
        if self._current_user is None:
            raise AuthenticationError("Сначала выполните login")
        return self._current_user

    @log_action("DEPOSIT", verbose=True)
    def deposit(self, amount: float) -> OperationResult:
        """Add virtual USD funds and record the deposit in the same file write."""
        user = self._require_user()
        return self._apply_operation(
            user, "DEPOSIT", "USD", validate_amount(amount), 1.0
        )

    @log_action("BUY", verbose=True)
    def buy(self, currency_code: str, amount: float) -> OperationResult:
        """Buy currency using available USD at a freshly obtained exchange rate."""
        return self._trade("BUY", currency_code, amount)

    @log_action("SELL", verbose=True)
    def sell(self, currency_code: str, amount: float) -> OperationResult:
        """Sell available currency and receive the USD proceeds atomically."""
        return self._trade("SELL", currency_code, amount)

    def _trade(self, action: str, currency_code: str, amount: float) -> OperationResult:
        user = self._require_user()
        code = get_currency(currency_code).code
        amount = validate_amount(amount)
        if code == "USD":
            raise ValidationError(
                "Обмен USD на USD недопустим. Для пополнения используйте deposit"
            )
        rate = validate_amount(self._rate_provider(code, "USD"))
        return self._apply_operation(user, action, code, amount, rate)

    def _apply_operation(
        self, user: UserInfo, action: str, code: str, amount: float, rate: float
    ) -> OperationResult:
        set_action_quote(rate, "USD")
        total = validate_amount(amount * rate)
        result = None

        def mutate(portfolio: Portfolio) -> None:
            nonlocal result
            wallet = portfolio.get_wallet(code)
            usd_wallet = portfolio.get_wallet("USD")
            before = wallet.balance if wallet is not None else 0.0
            usd_before = usd_wallet.balance if usd_wallet is not None else 0.0
            if action == "DEPOSIT":
                changes = (BalanceChange("USD", usd_before, usd_before + amount),)
            else:
                if action == "BUY" and usd_before < total:
                    raise InsufficientFundsError(usd_before, total, "USD")
                if action == "SELL":
                    if wallet is None:
                        raise ValidationError(
                            f"У вас нет кошелька '{code}'. "
                            "Он создаётся автоматически при первой покупке."
                        )
                    if before < amount:
                        raise InsufficientFundsError(before, amount, code)
                direction = 1 if action == "BUY" else -1
                changes = (
                    BalanceChange(code, before, before + direction * amount),
                    BalanceChange("USD", usd_before, usd_before - direction * total),
                )
            for change in changes:
                validate_number(change.after, field="Баланс", allow_zero=True)
                if change.before == change.after:
                    raise ValidationError(
                        "Сумма слишком мала для изменения текущего баланса"
                    )
            transaction = Transaction(
                id=str(uuid4()),
                action=action,
                currency_code=code,
                amount=amount,
                rate=rate,
                base="USD",
                total=total,
                timestamp=datetime.now(UTC),
            )
            for change in changes:
                portfolio.add_currency(change.currency_code).balance = change.after
            portfolio.add_transaction(transaction)
            result = OperationResult(
                user.user_id, portfolio.user.username, transaction, changes
            )

        self._database.update_portfolio(
            user.user_id, mutate, rate_provider=self._rate_provider
        )
        assert result is not None
        return result
