"""CLI errors and successful trades produce consistent action diagnostics."""

import io
import json
from unittest.mock import Mock

import pytest

from valutatrade_hub.cli.interface import main, run_repl
from valutatrade_hub.core.exceptions import StorageError
from valutatrade_hub.core.usecases import TradingService
from valutatrade_hub.infra.settings import SettingsLoader
from valutatrade_hub.logging_config import configure_logging


def test_cli_keeps_working_after_failed_trade_and_logs_only_trade_commands(tmp_path):
    directory = tmp_path / "data"
    output, errors = io.StringIO(), io.StringIO()
    assert (
        main(
            ["--data-dir", str(directory)],
            stdin=io.StringIO(
                "register --username alice --password hidden-password\n"
                "login --username alice --password hidden-password\n"
                "deposit --amount 100\n"
                "buy --currency BTC --amount 1\n"
                "logout\ndeposit --amount 10\nexit\n"
            ),
            stdout=output,
            stderr=errors,
        )
        == 0
    )
    events = [
        json.loads(line)
        for line in (tmp_path / "logs" / "actions.log").read_text("utf-8").splitlines()
    ]
    assert [(event["action"], event["result"]) for event in events] == [
        ("DEPOSIT", "OK"),
        ("BUY", "ERROR"),
        ("DEPOSIT", "ERROR"),
    ]
    assert events[1]["error_type"] == "RateUnavailableError"
    assert events[2]["error_type"] == "AuthenticationError"
    assert "Нет актуального курса" in errors.getvalue()
    assert "Сначала выполните login" in errors.getvalue()
    assert (
        "hidden-password"
        not in json.dumps(events) + output.getvalue() + errors.getvalue()
    )
    assert "Traceback" not in errors.getvalue()


def test_storage_exception_reaches_cli_and_log_without_secret_url(
    database, saved_account, monkeypatch
):
    settings = SettingsLoader()
    configure_logging(settings)
    service = TradingService(database, rate_provider=lambda source, target: 60000)
    service.login("Алиса", "secret")
    failure = StorageError("Сбой https://example.test/v6/hidden-key/latest/USD")
    monkeypatch.setattr(database, "update_portfolio", Mock(side_effect=failure))
    output, errors = io.StringIO(), io.StringIO()
    assert (
        run_repl(
            service,
            default_base="USD",
            stdin=io.StringIO("buy --currency BTC --amount 0.01\nexit\n"),
            stdout=output,
            stderr=errors,
        )
        == 0
    )
    event = json.loads((settings.get("log_dir") / "actions.log").read_text("utf-8"))
    assert event["error_type"] == "StorageError"
    assert event["result"] == "ERROR"
    assert event["error_message"] in errors.getvalue()
    assert "hidden-key" not in errors.getvalue() + json.dumps(event)
    assert "Покупка выполнена" not in output.getvalue()


@pytest.mark.parametrize(
    "arguments", [["--help"], ["help"], ["buy", "--help"], ["exit"]]
)
def test_help_and_exit_do_not_initialize_logging(tmp_path, arguments):
    assert (
        main(
            ["--data-dir", str(tmp_path / "data"), *arguments],
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
        == 0
    )
    assert list(tmp_path.iterdir()) == []
