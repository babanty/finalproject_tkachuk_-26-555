"""Exercise command streams and independent CLI processes without real data."""

import io
import json
import os
import subprocess
import sys

import pytest

from valutatrade_hub.cli.interface import main, run_repl
from valutatrade_hub.core.usecases import TradingService
from valutatrade_hub.infra.json_io import read_json


def invoke(arguments, commands=""):
    output, errors = io.StringIO(), io.StringIO()
    status = main(arguments, stdin=io.StringIO(commands), stdout=output, stderr=errors)
    return status, output.getvalue(), errors.getvalue()


def test_repl_recovers_from_errors_and_preserves_session(tmp_path):
    directory = tmp_path / "данные с пробелами"
    commands = "\n".join(
        [
            "",
            "unknown",
            'login --username "bad',
            "register --username Алиса",
            "--help",
            "register --help",
            "help",
            'register --username "Алиса Тест" --password " spaced password "',
            "show-portfolio",
            'login --username "Алиса Тест" --password " spaced password "',
            "show-portfolio --base eur",
            "show-portfolio --base ABC",
            'login --username "Алиса Тест" --password wrong',
            "show-portfolio",
            'login --username "Алиса Тест" --password " spaced password "',
            "logout",
            "show-portfolio",
            "exit",
            "register --username never --password secret",
        ]
    )
    status, output, errors = invoke(["--data-dir", str(directory)], commands)
    assert status == 0
    assert "зарегистрирован (id=1)" in output
    assert "Портфель пуст" in output
    assert "ИТОГО: 0.00 EUR" in output
    assert output.count("Вы вошли как 'Алиса Тест'") == 2
    assert "Вы вышли из системы" in output
    assert "Некорректные аргументы" in errors
    assert "проверьте кавычки" in errors
    assert "Неверный пароль" in errors
    assert "Неизвестная валюта 'ABC'" in errors
    assert errors.count("Сначала выполните login") == 3
    assert "Traceback" not in errors
    assert "spaced password" not in output + errors
    assert len(read_json(directory / "users.json")) == 1


@pytest.mark.parametrize(
    "arguments", [["--help"], ["help"], ["register", "--help"], ["exit"]]
)
def test_help_and_exit_do_not_create_data(tmp_path, arguments):
    directory = tmp_path / "unused"
    status, output, errors = invoke(["--data-dir", str(directory), *arguments])
    assert status == 0
    assert not errors
    assert not directory.exists()


@pytest.mark.parametrize(
    "arguments",
    [
        ["unknown-secret"],
        ["register", "--username", "Алиса", "--password", "secret", "extra-secret"],
        ["login", "--password", "secret"],
    ],
)
def test_invalid_arguments_do_not_echo_input_or_create_data(tmp_path, arguments):
    directory = tmp_path / "unused"
    status, output, errors = invoke(["--data-dir", str(directory), *arguments])
    assert status == 1
    assert "Некорректные аргументы" in errors
    assert "secret" not in output + errors
    assert not directory.exists()


def test_cli_config_and_explicit_data_directory(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"data_dir": str(tmp_path / "unused"), "base_currency": "EUR"}),
        encoding="utf-8",
    )
    directory = tmp_path / "chosen"
    status, output, errors = invoke(
        ["--config", str(config), "--data-dir", str(directory)],
        "register --username alice --password secret\n"
        "login --username alice --password secret\nshow-portfolio\n",
    )
    assert status == 0
    assert not errors
    assert "ИТОГО: 0.00 EUR" in output
    assert directory.is_dir()
    assert not (tmp_path / "unused").exists()


@pytest.mark.parametrize("commands", ["", "exit\n"])
def test_repl_exit_clears_session(database, saved_account, commands):
    service = TradingService(database, rate_provider=lambda source, target: 1.0)
    service.login("Алиса", "secret")
    assert (
        run_repl(
            service,
            default_base="USD",
            stdin=io.StringIO(commands),
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
        == 0
    )
    assert service.current_user is None


def test_keyboard_interrupt_exits_cleanly(tmp_path):
    class InterruptedInput(io.StringIO):
        def readline(self, *args):
            raise KeyboardInterrupt

    output = io.StringIO()
    assert (
        main(
            ["--data-dir", str(tmp_path / "data")],
            stdin=InterruptedInput(),
            stdout=output,
            stderr=io.StringIO(),
        )
        == 130
    )
    assert "Работа завершена" in output.getvalue()


def test_corrupt_storage_reports_error_without_overwriting(tmp_path):
    directory = tmp_path / "data"
    directory.mkdir()
    users = directory / "users.json"
    users.write_text("{broken", encoding="utf-8")
    status, output, errors = invoke(["--data-dir", str(directory)])
    assert status == 1
    assert "Ошибка:" in errors
    assert "Traceback" not in errors
    assert users.read_text("utf-8") == "{broken"
    assert not (directory / "portfolios.json").exists()


def test_separate_process_cannot_reuse_login(tmp_path):
    environment = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    command = [
        sys.executable,
        "-m",
        "valutatrade_hub",
        "--data-dir",
        str(tmp_path / "data"),
    ]
    scenarios = [
        (
            ["register", "--username", "Алиса", "--password", "secret"],
            0,
            "зарегистрирован",
        ),
        (["login", "--username", "Алиса", "--password", "secret"], 0, "Вы вошли"),
        (["show-portfolio"], 1, "Сначала выполните login"),
    ]
    for arguments, expected_status, message in scenarios:
        result = subprocess.run(
            [*command, *arguments],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
            check=False,
        )
        assert result.returncode == expected_status, result.stderr
        assert message in result.stdout + result.stderr
        assert "secret" not in result.stdout + result.stderr


def test_empty_base_is_not_replaced_by_default(tmp_path):
    status, output, errors = invoke(
        ["--data-dir", str(tmp_path / "data")],
        "register --username alice --password secret\n"
        'login --username alice --password secret\nshow-portfolio --base ""\n',
    )
    assert status == 0
    assert "Ошибка:" in errors
    assert "ИТОГО" not in output


def test_real_process_runs_interactive_commands_from_stdin(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "valutatrade_hub"],
        input='register --username "Алиса Тест" --password "test password"\n'
        'login --username "Алиса Тест" --password "test password"\n'
        "show-portfolio\nlogout\nexit\n",
        cwd=tmp_path,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not result.stderr
    assert "Вы вошли как 'Алиса Тест'" in result.stdout
    assert "Портфель пуст" in result.stdout
    assert "Вы вышли из системы" in result.stdout
    assert "test password" not in result.stdout
