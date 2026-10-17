"""Cached configuration with explicit precedence and no import-time I/O."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path
from threading import RLock
from typing import Any, ClassVar

from valutatrade_hub.core.currencies import get_currency
from valutatrade_hub.core.exceptions import StorageError, ValidationError
from valutatrade_hub.infra.json_io import read_json

ENV_PREFIX = "VALUTATRADE_"


def _positive_integer(value: Any, key: str) -> int:
    if isinstance(value, str):
        try:
            value = int(value)
        except ValueError as error:
            raise ValidationError(
                f"Настройка '{key}' должна быть целым числом > 0"
            ) from error
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationError(f"Настройка '{key}' должна быть целым числом > 0")
    return value


def _positive_number(value: Any, key: str) -> float:
    try:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            raise ValueError
    except (ValueError, OverflowError) as error:
        raise ValidationError(
            f"Настройка '{key}' должна быть конечным числом > 0"
        ) from error
    return number


def _directory(value: Any, key: str) -> Path:
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise ValidationError(f"Настройка '{key}' должна содержать путь")
    try:
        return Path(value).expanduser().resolve()
    except (OSError, ValueError, RuntimeError) as error:
        raise ValidationError(f"Некорректный путь в настройке '{key}'") from error


class SettingsLoader:
    """Use __new__ for one cached configuration without a custom metaclass."""

    _instance: ClassVar[SettingsLoader | None] = None
    _creation_lock = RLock()

    def __new__(cls, config_path=None, *, overrides=None):
        with cls._creation_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
            return cls._instance

    def __init__(
        self,
        config_path: str | Path | None = None,
        *,
        overrides: Mapping[str, Any] | None = None,
    ) -> None:
        with self._creation_lock:
            self._configure(config_path, overrides=overrides)

    def _configure(self, config_path, *, overrides) -> None:
        requested_path = (
            Path(config_path).resolve() if config_path is not None else None
        )
        if overrides is not None and not isinstance(overrides, Mapping):
            raise ValidationError("Явные настройки должны быть словарём")
        requested_overrides = dict(overrides or {})
        if getattr(self, "_initialized", False):
            if config_path is not None or overrides is not None:
                if (requested_path, requested_overrides) != self._specification:
                    raise ValidationError(
                        "SettingsLoader уже загружен с другой конфигурацией"
                    )
            return
        defaults = json.loads(
            files("valutatrade_hub").joinpath("default_config.json").read_text("utf-8")
        )
        values = defaults.copy()
        if requested_path is not None:
            try:
                configured = read_json(requested_path)
            except StorageError as error:
                raise ValidationError(
                    "Не удалось загрузить файл конфигурации"
                ) from error
            if not isinstance(configured, dict):
                raise ValidationError("Конфигурация должна быть JSON-объектом")
            values.update(configured)
        for key in defaults:
            environment_value = os.environ.get(ENV_PREFIX + key.upper())
            if environment_value is not None:
                values[key] = environment_value
        values.update(requested_overrides)
        if values.keys() - defaults.keys():
            raise ValidationError("Конфигурация содержит неизвестные настройки")
        for key in (
            "rates_ttl_seconds",
            "update_interval_seconds",
            "log_max_bytes",
            "log_backup_count",
        ):
            values[key] = _positive_integer(values[key], key)
        for key in ("request_timeout_seconds", "lock_timeout_seconds"):
            values[key] = _positive_number(values[key], key)
        values["base_currency"] = get_currency(values["base_currency"]).code
        if not isinstance(values["log_level"], str):
            raise ValidationError("Некорректный уровень логирования")
        values["log_level"] = values["log_level"].upper()
        if values["log_level"] not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValidationError("Некорректный уровень логирования")
        if values["log_format"] != "json":
            raise ValidationError("Поддерживается формат логов 'json'")
        values["data_dir"] = _directory(values["data_dir"], "data_dir")
        values["log_dir"] = (
            values["data_dir"].parent / "logs"
            if values["log_dir"] is None
            else _directory(values["log_dir"], "log_dir")
        )
        for key, name in (
            ("users_file", "users.json"),
            ("portfolios_file", "portfolios.json"),
            ("rates_file", "rates.json"),
            ("history_file", "exchange_rates.json"),
        ):
            values[key] = values["data_dir"] / name
        self._values = values
        self._specification = requested_path, requested_overrides
        self._initialized = True

    def get(self, key: str, default: Any = None) -> Any:
        """Return a typed configuration value or the supplied fallback."""
        return self._values.get(key, default)
