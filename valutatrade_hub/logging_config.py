"""Explicit JSON action logging with bounded files and safe diagnostics."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from threading import RLock

from valutatrade_hub.core.exceptions import StorageError, ValutaTradeError
from valutatrade_hub.infra.settings import SettingsLoader

_LOGGER = logging.getLogger("valutatrade_hub.actions")
_LOGGER.addHandler(logging.NullHandler())
_LOGGER.propagate = False
_LOGGER.setLevel(logging.INFO)
_LOCK = RLock()
_CONFIGURATION: tuple | None = None
_HANDLER: RotatingFileHandler | None = None
_PARSER_LOGGER = logging.getLogger("valutatrade_hub.parser")
_PARSER_LOGGER.addHandler(logging.NullHandler())
_PARSER_LOGGER.propagate = False
_PARSER_LOGGER.setLevel(logging.INFO)
_PARSER_HANDLER: RotatingFileHandler | None = None
_PARSER_CONFIGURATION: tuple | None = None
_FIELDS = frozenset(
    {
        "timestamp",
        "action",
        "user_id",
        "username",
        "currency_code",
        "amount",
        "rate",
        "base",
        "total",
        "transaction_id",
        "result",
        "error_type",
        "error_message",
        "before",
        "after",
    }
)
_SECRET_NAMES = ("COINGECKO_API_KEY", "EXCHANGERATE_API_KEY")
_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_CREDENTIAL = re.compile(
    r"\b(password|hashed_password|salt|api[_-]?key|x[-_]cg[-_]demo[-_]api[-_]key)"
    r"[\"']?\s*[:=]\s*(\"[^\"]*\"|'[^']*'|[^\s,;]+)",
    re.IGNORECASE,
)


def safe_text(value: str) -> str:
    """Redact configured API keys, URLs, and named credentials in diagnostics."""
    for name in _SECRET_NAMES:
        secret = os.environ.get(name)
        if secret:
            value = value.replace(secret, "[скрыто]")
    value = _URL.sub("[URL скрыт]", value)
    return _CREDENTIAL.sub(lambda match: f"{match[1]}=[скрыто]", value)


def safe_error_message(error: Exception) -> str:
    """Keep domain diagnostics without dumping arbitrary transport exceptions."""
    if isinstance(error, ValutaTradeError):
        return safe_text(str(error))
    return "Внутренняя ошибка операции"


class JsonActionFormatter(logging.Formatter):
    """Write one allowlisted event per line without arguments or tracebacks."""

    fields = _FIELDS

    def format(self, record: logging.LogRecord) -> str:
        """Serialize only the structured action fields supplied by the decorator."""
        event = getattr(record, "action_event", {})
        return json.dumps(
            {key: value for key, value in event.items() if key in self.fields},
            ensure_ascii=False,
            allow_nan=False,
        )


class JsonParserFormatter(JsonActionFormatter):
    """Restrict parser diagnostics to outcomes and committed price changes."""

    fields = frozenset(
        {
            "timestamp",
            "event",
            "source",
            "result",
            "pair",
            "before",
            "after",
            "delta",
            "updated_at",
            "success_sources",
            "errors",
            "updated_count",
            "added_count",
            "last_refresh",
            "error_type",
            "error_message",
        }
    )


class ActionFileHandler(RotatingFileHandler):
    """Keep a logging I/O failure from changing an already committed operation."""

    def handleError(self, record: logging.LogRecord) -> None:
        """Avoid secondary tracebacks and exposing the record on logging failure."""


def shutdown_logging() -> None:
    """Release the owned file handle, including before Windows reconfiguration."""
    global _HANDLER, _CONFIGURATION
    global _PARSER_HANDLER, _PARSER_CONFIGURATION
    with _LOCK:
        if _HANDLER is not None:
            _LOGGER.removeHandler(_HANDLER)
            _HANDLER.close()
        _HANDLER = None
        _CONFIGURATION = None
        if _PARSER_HANDLER is not None:
            _PARSER_LOGGER.removeHandler(_PARSER_HANDLER)
            _PARSER_HANDLER.close()
        _PARSER_HANDLER = None
        _PARSER_CONFIGURATION = None


def configure_logging(settings: SettingsLoader) -> None:
    """Configure the action logger once per settings tuple, leaving root alone."""
    global _HANDLER, _CONFIGURATION
    directory = settings.get("log_dir")
    configuration = (
        directory,
        settings.get("log_level"),
        settings.get("log_max_bytes"),
        settings.get("log_backup_count"),
    )
    with _LOCK:
        if configuration == _CONFIGURATION and _HANDLER in _LOGGER.handlers:
            return
        if _HANDLER is not None:
            _LOGGER.removeHandler(_HANDLER)
            _HANDLER.close()
        _HANDLER = None
        _CONFIGURATION = None
        try:
            directory.mkdir(parents=True, exist_ok=True)
            handler = ActionFileHandler(
                directory / "actions.log",
                maxBytes=settings.get("log_max_bytes"),
                backupCount=settings.get("log_backup_count"),
                encoding="utf-8",
            )
        except (OSError, ValueError) as error:
            raise StorageError("Не удалось настроить журнал действий") from error
        handler.setFormatter(JsonActionFormatter())
        _LOGGER.setLevel(settings.get("log_level"))
        _LOGGER.addHandler(handler)
        _HANDLER = handler
        _CONFIGURATION = configuration


def write_action(event: dict) -> None:
    """Best-effort logging never replaces an operation's result or exception."""
    with _LOCK:
        try:
            _LOGGER.info("action", extra={"action_event": event})
        except Exception:
            pass


def configure_parser_logging(settings: SettingsLoader) -> None:
    """Configure parser.log independently, using the same rotation settings."""
    global _PARSER_HANDLER, _PARSER_CONFIGURATION
    directory = settings.get("log_dir")
    configuration = (
        directory,
        settings.get("log_level"),
        settings.get("log_max_bytes"),
        settings.get("log_backup_count"),
    )
    with _LOCK:
        if (
            configuration == _PARSER_CONFIGURATION
            and _PARSER_HANDLER in _PARSER_LOGGER.handlers
        ):
            return
        if _PARSER_HANDLER is not None:
            _PARSER_LOGGER.removeHandler(_PARSER_HANDLER)
            _PARSER_HANDLER.close()
        _PARSER_HANDLER = None
        _PARSER_CONFIGURATION = None
        try:
            directory.mkdir(parents=True, exist_ok=True)
            handler = ActionFileHandler(
                directory / "parser.log",
                maxBytes=settings.get("log_max_bytes"),
                backupCount=settings.get("log_backup_count"),
                encoding="utf-8",
            )
        except (OSError, ValueError):
            raise StorageError(
                "Не удалось настроить журнал обновления курсов"
            ) from None
        handler.setFormatter(JsonParserFormatter())
        _PARSER_LOGGER.setLevel(settings.get("log_level"))
        _PARSER_LOGGER.addHandler(handler)
        _PARSER_HANDLER = handler
        _PARSER_CONFIGURATION = configuration


def write_parser(event: dict) -> None:
    """Emit a parser diagnostic without changing a persisted update's outcome."""
    with _LOCK:
        try:
            payload = {
                **event,
                "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            }
            _PARSER_LOGGER.info("parser", extra={"action_event": payload})
        except Exception:
            pass
