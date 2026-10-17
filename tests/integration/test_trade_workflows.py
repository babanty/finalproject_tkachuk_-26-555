"""Run the complete funded trading scenario through actual CLI processes."""

import io
import os
import subprocess
import sys
from datetime import UTC, datetime

import pytest

from valutatrade_hub.cli.interface import main
from valutatrade_hub.infra.json_io import atomic_write_json, read_json


def run_cli(directory, commands, cwd):
    return subprocess.run(
        [sys.executable, "-m", "valutatrade_hub", "--data-dir", str(directory)],
        input=commands,
        cwd=cwd,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        check=False,
    )


def test_full_cli_trade_cycle_and_restart_keep_balances_and_history(tmp_path):
    directory = tmp_path / "данные с пробелами"
    directory.mkdir()
    timestamp = datetime.now(UTC).isoformat()
    atomic_write_json(
        directory / "rates.json",
        {
            "pairs": {
                f"{code}_USD": {
                    "rate": rate,
                    "updated_at": timestamp,
                    "source": "test",
                }
                for code, rate in {"BTC": 60000, "EUR": 1.25}.items()
            },
            "last_refresh": timestamp,
        },
    )
    result = run_cli(
        directory,
        'register --username "Алиса Тест" --password secret\n'
        'login --username "Алиса Тест" --password secret\n'
        "buy --currency BTC --amount 0.05\n"
        "deposit --amount wrong\ndeposit --amount nan\n"
        "deposit --amount 10000\nbuy --currency btc --amount 0.05\n"
        "sell --currency BTC --amount 0.01\nshow-portfolio\n"
        "show-portfolio --base EUR\nlogout\ndeposit --amount 10\nexit\n",
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert "Виртуальный баланс пополнен: 10000 USD" in result.stdout
    assert "Покупка выполнена: 0.05 BTC" in result.stdout
    assert "Продажа выполнена: 0.01 BTC" in result.stdout
    assert "- USD: было 10000 → стало 7000" in result.stdout
    assert "- USD: было 7000 → стало 7600" in result.stdout
    assert "ИТОГО: 10,000.00 USD" in result.stdout
    assert "ИТОГО: 8,000.00 EUR" in result.stdout
    assert "Недостаточно средств" in result.stderr
    assert "Некорректные аргументы" in result.stderr
    assert "'amount' должен быть положительным числом" in result.stderr
    assert "Сначала выполните login" in result.stderr
    assert "secret" not in result.stdout + result.stderr
    before = (directory / "portfolios.json").read_bytes()
    second = run_cli(
        directory,
        'show-portfolio\nlogin --username "Алиса Тест" --password secret\n'
        "show-portfolio\nexit\n",
        tmp_path,
    )
    assert second.returncode == 0
    assert "Сначала выполните login" in second.stderr
    assert "ИТОГО: 10,000.00 USD" in second.stdout
    assert (directory / "portfolios.json").read_bytes() == before
    records = read_json(directory / "portfolios.json")
    assert len(records[0]["transactions"]) == 3
    assert records[0]["wallets"]["USD"]["balance"] == 7600
    assert records[0]["wallets"]["BTC"]["balance"] == pytest.approx(0.04)


@pytest.mark.parametrize("command", ["deposit", "buy", "sell"])
def test_trade_help_does_not_initialize_data(tmp_path, command):
    output, errors = io.StringIO(), io.StringIO()
    directory = tmp_path / "unused"
    assert (
        main(
            ["--data-dir", str(directory), command, "--help"],
            stdout=output,
            stderr=errors,
        )
        == 0
    )
    assert "--amount" in output.getvalue()
    assert not directory.exists()
    assert not errors.getvalue()


@pytest.mark.parametrize("command", ["deposit", "buy", "sell"])
def test_separate_trade_command_requires_login(tmp_path, command):
    arguments = [command, "--amount", "1"]
    if command != "deposit":
        arguments.extend(["--currency", "BTC"])
    output, errors = io.StringIO(), io.StringIO()
    assert (
        main(
            ["--data-dir", str(tmp_path / "data"), *arguments],
            stdout=output,
            stderr=errors,
        )
        == 1
    )
    assert "Сначала выполните login" in errors.getvalue()
    assert not output.getvalue()
