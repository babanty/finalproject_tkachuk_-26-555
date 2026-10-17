"""Errors that the application can display to its users."""


class ValutaTradeError(Exception):
    """Base class for expected application errors."""


class ValidationError(ValutaTradeError, ValueError):
    """An input value violates a domain rule."""


class StorageError(ValutaTradeError):
    """Local data could not be read, validated, locked, or saved."""


class AuthenticationError(ValutaTradeError):
    """Credentials are invalid or an operation requires a signed-in user."""


class RateUnavailableError(ValutaTradeError):
    """Required currency-to-USD quotes are missing, expired, or future-dated."""

    def __init__(self, currencies: frozenset[str]) -> None:
        self.currencies = currencies
        pairs = ", ".join(f"{code}_USD" for code in sorted(currencies))
        super().__init__(
            f"Нет актуального курса для {pairs}: котировка отсутствует, "
            "устарела или датирована будущим. Обновите локальный кэш курсов."
        )


class InsufficientFundsError(ValutaTradeError):
    """A wallet cannot cover the requested withdrawal."""

    def __init__(self, available: float, required: float, code: str) -> None:
        self.available = available
        self.required = required
        self.code = code
        super().__init__(
            f"Недостаточно средств: доступно {available:g} {code}, "
            f"требуется {required:g} {code}"
        )


class CurrencyNotFoundError(ValutaTradeError):
    """A syntactically valid currency code is not supported."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"Неизвестная валюта '{code}'")


class ApiRequestError(ValutaTradeError):
    """An exchange-rate provider could not supply usable data."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Ошибка при обращении к внешнему API: {reason}")
