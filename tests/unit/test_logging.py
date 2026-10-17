"""Action events describe persisted outcomes without exposing credentials."""

import json
import logging
from datetime import UTC, datetime
from inspect import signature
from unittest.mock import Mock

import pytest

from valutatrade_hub.core.exceptions import (
    AuthenticationError,
    InsufficientFundsError,
    RateUnavailableError,
    StorageError,
    ValidationError,
)
from valutatrade_hub.core.usecases import TradingService
from valutatrade_hub.decorators import log_action
from valutatrade_hub.infra import json_io
from valutatrade_hub.infra.settings import SettingsLoader
from valutatrade_hub.logging_config import (
    ActionFileHandler,
    configure_logging,
    safe_error_message,
    shutdown_logging,
)


def read_events(directory):
    return [
        json.loads(line)
        for line in (directory / "actions.log").read_text("utf-8").splitlines()
    ]


@pytest.fixture
def logged_service(database):
    settings = SettingsLoader()
    configure_logging(settings)
    service = TradingService(database, rate_provider=lambda source, target: 60000)
    service.register("Алиса", "private-password")
    service.login("Алиса", "private-password")
    return service, settings.get("log_dir")


def test_events_match_persisted_transactions_and_verbose_balance_changes(
    database, logged_service
):
    service, directory = logged_service
    results = [
        service.deposit(10000),
        service.buy("btc", 0.05),
        service.sell("BTC", 0.01),
    ]
    events = read_events(directory)
    assert len(events) == 3
    for event, result in zip(events, results, strict=True):
        transaction = result.transaction
        assert event["action"] == transaction.action
        assert event["result"] == "OK"
        assert event["user_id"] == 1
        assert event["username"] == "Алиса"
        assert event["currency_code"] == transaction.currency_code
        assert event["amount"] == transaction.amount
        assert event["rate"] == transaction.rate
        assert event["base"] == "USD"
        assert event["total"] == transaction.total
        assert event["transaction_id"] == transaction.id
        assert event["error_type"] is event["error_message"] is None
        assert datetime.fromisoformat(event["timestamp"]).utcoffset() == UTC.utcoffset(
            None
        )
        assert event["before"] == {
            change.currency_code: change.before for change in result.changes
        }
        assert event["after"] == {
            change.currency_code: change.after for change in result.changes
        }
    saved = database.get_portfolio(1, rate_provider=lambda source, target: 60000)
    assert [item.id for item in saved.transactions] == [
        event["transaction_id"] for event in events
    ]


def test_insufficient_funds_keeps_quote_and_original_exception(logged_service):
    service, directory = logged_service
    with pytest.raises(InsufficientFundsError) as captured:
        service.buy(currency_code="btc", amount=0.05)
    (event,) = read_events(directory)
    assert event["result"] == "ERROR"
    assert event["error_type"] == "InsufficientFundsError"
    assert event["error_message"] == str(captured.value)
    assert event["rate"] == 60000
    assert event["base"] == "USD"
    assert event["amount"] == 0.05
    assert event["currency_code"] == "BTC"
    assert "transaction_id" not in event
    assert "after" not in event


@pytest.mark.parametrize("failure", ["replace", "fsync"])
def test_portfolio_write_failure_never_logs_success(
    database, logged_service, failure, monkeypatch
):
    service, directory = logged_service
    service.deposit(10000)
    before = database.get_path("portfolios").read_bytes()
    monkeypatch.setattr(
        json_io.os, failure, Mock(side_effect=PermissionError("private"))
    )
    with pytest.raises(StorageError):
        service.buy("BTC", 0.05)
    events = read_events(directory)
    assert [event["result"] for event in events] == ["OK", "ERROR"]
    assert events[-1]["error_type"] == "StorageError"
    assert events[-1]["rate"] == 60000
    assert database.get_path("portfolios").read_bytes() == before


@pytest.mark.parametrize(
    "amount", [float("nan"), float("inf"), True, "private-password"]
)
def test_invalid_inputs_produce_valid_json_without_raw_arguments(
    logged_service, amount
):
    service, directory = logged_service
    with pytest.raises(ValidationError):
        service.deposit(amount)
    (event,) = read_events(directory)
    assert event["result"] == "ERROR"
    assert event["amount"] is None
    assert event["rate"] is None
    assert "private-password" not in (directory / "actions.log").read_text("utf-8")


def test_unauthenticated_failure_and_missing_quote_do_not_reuse_context(
    logged_service, monkeypatch
):
    service, directory = logged_service
    service.deposit(100)
    missing = RateUnavailableError(frozenset({"BTC"}))
    monkeypatch.setattr(service, "_rate_provider", Mock(side_effect=missing))
    with pytest.raises(RateUnavailableError) as captured:
        service.buy("BTC", 0.01)
    assert captured.value is missing
    service.logout()
    with pytest.raises(AuthenticationError):
        service.sell("BTC", 1)
    events = read_events(directory)
    assert events[1]["rate"] is None
    assert events[2]["rate"] is None
    assert events[2]["user_id"] is events[2]["username"] is None
    assert events[2]["error_type"] == "AuthenticationError"


def test_nested_actions_restore_the_outer_quote_context(logged_service, monkeypatch):
    service, directory = logged_service

    def quote(source, target):
        service.deposit(10000)
        return 60000

    monkeypatch.setattr(service, "_rate_provider", quote)
    service.buy("BTC", 0.05)
    deposit, purchase = read_events(directory)
    assert deposit["action"] == "DEPOSIT"
    assert deposit["rate"] == 1
    assert purchase["action"] == "BUY"
    assert purchase["rate"] == 60000


def test_decorator_preserves_metadata_signature_result_and_non_verbose_mode(
    logged_service,
):
    service, directory = logged_service
    result = service.deposit(10)

    @log_action("DEPOSIT")
    def operation(amount: float):
        """Return a previously persisted result."""
        return result

    assert operation.__name__ == "operation"
    assert operation.__doc__ == "Return a previously persisted result."
    assert signature(operation) == signature(operation.__wrapped__)
    assert operation(amount=10) is result
    event = read_events(directory)[-1]
    assert event["result"] == "OK"
    assert "before" not in event
    assert "after" not in event
    assert TradingService.buy.__wrapped__.__name__ == "buy"


def test_repeated_configuration_emits_one_event_and_leaves_root_logger_alone(
    logged_service,
):
    service, directory = logged_service
    root = logging.getLogger()
    before = root.level, root.handlers[:]
    for iteration in range(4):
        configure_logging(SettingsLoader())
    service.deposit(1)
    assert len(read_events(directory)) == 1
    assert (root.level, root.handlers[:]) == before


def test_reconfiguration_closes_previous_handle_and_switches_directory(
    tmp_path, logged_service, monkeypatch
):
    service, old_directory = logged_service
    service.deposit(1)
    logger = logging.getLogger("valutatrade_hub.actions")
    old_handler = next(
        item for item in logger.handlers if isinstance(item, ActionFileHandler)
    )
    monkeypatch.setattr(SettingsLoader, "_instance", None)
    settings = SettingsLoader(overrides={"log_dir": tmp_path / "другой журнал"})
    configure_logging(settings)
    assert old_handler.stream is None
    old_file = old_directory / "actions.log"
    old_file.rename(old_directory / "moved.log")
    service.deposit(2)
    assert read_events(settings.get("log_dir"))[0]["amount"] == 2
    assert not old_file.exists()


def test_small_log_limit_rotates_valid_json_and_limits_backups(
    database, tmp_path, monkeypatch
):
    monkeypatch.setattr(SettingsLoader, "_instance", None)
    settings = SettingsLoader(
        overrides={
            "log_dir": tmp_path / "журнал",
            "log_max_bytes": 1000,
            "log_backup_count": 2,
        }
    )
    configure_logging(settings)
    service = TradingService(database, rate_provider=lambda source, target: 1)
    service.register("Алиса", "private-password")
    service.login("Алиса", "private-password")
    for iteration in range(12):
        service.deposit(1)
    directory = settings.get("log_dir")
    assert {item.name for item in directory.iterdir()} == {
        "actions.log",
        "actions.log.1",
        "actions.log.2",
    }
    events = [
        json.loads(line)
        for path in directory.iterdir()
        for line in path.read_text("utf-8").splitlines()
    ]
    assert 0 < len(events) < 12
    assert all(event["result"] == "OK" for event in events)
    assert read_events(directory)[-1]["after"]["USD"] == 12


def test_credentials_are_absent_from_logs_and_raw_provider_errors_are_not_dumped(
    logged_service, database, monkeypatch
):
    service, directory = logged_service
    user = database.get_user(1)
    credentials = [
        "private-password",
        user.hashed_password,
        user.salt,
        "demo-key-private",
    ]
    monkeypatch.setenv("COINGECKO_API_KEY", credentials[-1])
    service.deposit(10)
    failure = RuntimeError(" ".join(credentials))
    monkeypatch.setattr(service, "_rate_provider", Mock(side_effect=failure))
    with pytest.raises(RuntimeError) as captured:
        service.buy("BTC", 1)
    assert captured.value is failure
    text = (directory / "actions.log").read_text("utf-8")
    assert all(secret not in text for secret in credentials)
    assert read_events(directory)[-1]["error_message"] == "Внутренняя ошибка операции"


def test_sensitive_domain_diagnostics_are_redacted(monkeypatch):
    monkeypatch.setenv("EXCHANGERATE_API_KEY", "exchange-private-key")
    error = StorageError(
        "exchange-private-key https://example.test/v6/secret/latest/USD "
        "password=private-password salt=private-salt hashed_password=private-hash "
        "api_key=private-api-key"
    )
    message = safe_error_message(error)
    assert "[скрыто]" in message
    assert "[URL скрыт]" in message
    for secret in (
        "exchange-private-key",
        "private-password",
        "private-salt",
        "private-hash",
        "private-api-key",
        "https://",
        "example.test",
    ):
        assert secret not in message


@pytest.mark.parametrize(
    "message",
    [
        '"password": "private password with spaces"',
        "'salt': 'private salt with spaces'",
        "x_cg_demo_api_key=private-key",
    ],
)
def test_quoted_credentials_and_header_variants_are_redacted(message):
    assert "private" not in safe_error_message(StorageError(message))


def test_embedded_newline_remains_one_json_record(logged_service):
    service, directory = logged_service
    service.logout()
    service.register("Имя\nВторая строка", "other-password")
    service.login("Имя\nВторая строка", "other-password")
    service.deposit(1)
    assert read_events(directory)[0]["username"] == "Имя\nВторая строка"


def test_log_write_failure_does_not_turn_a_saved_deposit_into_an_error(
    database, logged_service, monkeypatch, capsys
):
    service, directory = logged_service
    monkeypatch.setattr(
        ActionFileHandler, "doRollover", Mock(side_effect=OSError("private"))
    )
    monkeypatch.setattr(ActionFileHandler, "shouldRollover", lambda self, record: True)
    result = service.deposit(10)
    assert result.transaction.amount == 10
    saved = database.get_portfolio(1, rate_provider=lambda source, target: 1)
    assert saved.get_wallet("USD").balance == 10
    assert len(saved.transactions) == 1
    assert capsys.readouterr().err == ""


def test_logging_failure_preserves_original_operation_exception(
    logged_service, monkeypatch
):
    service, directory = logged_service
    logger = logging.getLogger("valutatrade_hub.actions")
    monkeypatch.setattr(logger, "info", Mock(side_effect=RuntimeError("logger failed")))
    with pytest.raises(InsufficientFundsError):
        service.buy("BTC", 1)


def test_unwritable_log_directory_reports_safe_configuration_error(tmp_path):
    path = tmp_path / "not-a-directory"
    path.write_text("occupied", encoding="utf-8")
    settings = SettingsLoader(overrides={"log_dir": path})
    with pytest.raises(StorageError, match="Не удалось настроить журнал действий"):
        configure_logging(settings)


def test_configured_level_controls_action_output(logged_service, monkeypatch):
    service, directory = logged_service
    monkeypatch.setattr(SettingsLoader, "_instance", None)
    configure_logging(
        SettingsLoader(overrides={"log_dir": directory, "log_level": "WARNING"})
    )
    service.deposit(1)
    assert read_events(directory) == []
    shutdown_logging()


def test_unsupported_decorator_action_is_rejected():
    with pytest.raises(ValueError, match="Неизвестный тип"):
        log_action("PASSWORD")
