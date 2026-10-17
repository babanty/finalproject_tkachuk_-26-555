"""Shared validation for domain values."""

import math
import re

from valutatrade_hub.core.exceptions import ValidationError


def normalize_currency_code(code: str) -> str:
    """Uppercase a code containing two to five ASCII letters or digits."""
    message = "Код валюты должен содержать 2–5 латинских букв или цифр без пробелов"
    if not isinstance(code, str) or not code.isascii():
        raise ValidationError(message)
    normalized = code.upper()
    if re.fullmatch(r"[A-Z0-9]{2,5}", normalized) is None:
        raise ValidationError(message)
    return normalized


def validate_text(value: str, field: str) -> str:
    """Trim descriptive text and reject missing or non-string values."""
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} не может быть пустым")
    return value.strip()


def validate_number(
    value: int | float, *, field: str, allow_zero: bool = False
) -> float:
    """Accept finite numeric values while rejecting booleans and strings."""
    qualifier = "неотрицательным" if allow_zero else "положительным"
    message = f"{field} должен быть {qualifier} числом"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(message)
    try:
        number = float(value)
    except OverflowError as error:
        raise ValidationError(message) from error
    if not math.isfinite(number) or number < 0 or (number == 0 and not allow_zero):
        raise ValidationError(message)
    return number


def validate_amount(amount: int | float) -> float:
    """Validate an amount that must be strictly positive."""
    return validate_number(amount, field="'amount'")


def validate_user_id(user_id: int) -> int:
    """Require a positive integer identifier, excluding booleans."""
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise ValidationError("Идентификатор пользователя должен быть целым числом > 0")
    return user_id
