"""Action logging around completed use cases, without recording raw arguments."""

from __future__ import annotations

import math
from collections.abc import Callable
from contextvars import ContextVar
from datetime import UTC, datetime
from functools import wraps
from inspect import signature
from typing import TYPE_CHECKING, ParamSpec, TypeVar

from valutatrade_hub.core.currencies import get_currency
from valutatrade_hub.core.exceptions import ValutaTradeError
from valutatrade_hub.logging_config import safe_error_message, safe_text, write_action

if TYPE_CHECKING:
    from valutatrade_hub.core.usecases import OperationResult

Parameters = ParamSpec("Parameters")
Result = TypeVar("Result", bound="OperationResult")
_CONTEXT: ContextVar[dict | None] = ContextVar("action_context", default=None)


def set_action_quote(rate: float, base: str) -> None:
    """Expose an obtained quote even when the subsequent portfolio write fails."""
    event = _CONTEXT.get()
    if event is not None:
        event.update(rate=rate, base=base)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except OverflowError:
        return None


def _currency(value: object) -> str | None:
    try:
        return get_currency(value).code
    except ValutaTradeError:
        return None


def log_action(action: str, verbose: bool = False):
    """Log one success or failure and preserve the wrapped callable's contract."""
    if action not in {"DEPOSIT", "BUY", "SELL"}:
        raise ValueError("Неизвестный тип журналируемой операции")

    def decorate(
        function: Callable[Parameters, Result],
    ) -> Callable[Parameters, Result]:
        parameters = signature(function)

        @wraps(function)
        def wrapped(*args: Parameters.args, **kwargs: Parameters.kwargs) -> Result:
            try:
                arguments = parameters.bind_partial(*args, **kwargs).arguments
            except TypeError:
                arguments = {}
            user = getattr(arguments.get("self"), "current_user", None)
            event = {
                "action": action,
                "user_id": user.user_id if user is not None else None,
                "username": safe_text(user.username) if user is not None else None,
                "currency_code": "USD"
                if action == "DEPOSIT"
                else _currency(arguments.get("currency_code")),
                "amount": _number(arguments.get("amount")),
                "rate": None,
                "base": "USD",
                "result": "ERROR",
                "error_type": None,
                "error_message": None,
            }
            token = _CONTEXT.set(event)
            try:
                result = function(*args, **kwargs)
            except Exception as error:
                event.update(
                    error_type=type(error).__name__,
                    error_message=safe_error_message(error),
                )
                raise
            else:
                transaction = result.transaction
                event.update(
                    user_id=result.user_id,
                    username=safe_text(result.username),
                    currency_code=transaction.currency_code,
                    amount=transaction.amount,
                    rate=transaction.rate,
                    base=transaction.base,
                    total=transaction.total,
                    transaction_id=transaction.id,
                    result="OK",
                )
                if verbose:
                    event["before"] = {
                        change.currency_code: change.before for change in result.changes
                    }
                    event["after"] = {
                        change.currency_code: change.after for change in result.changes
                    }
                return result
            finally:
                _CONTEXT.reset(token)
                event["timestamp"] = (
                    datetime.now(UTC).isoformat().replace("+00:00", "Z")
                )
                write_action(event)

        return wrapped

    return decorate
