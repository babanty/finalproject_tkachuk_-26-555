"""Exercise an isolated wheel installation outside the source tree, offline."""

import argparse
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory


def clean_environment() -> dict[str, str]:
    """Do not inherit application credentials, Python paths, or active environments."""
    excluded = {
        "COINGECKO_API_KEY",
        "EXCHANGERATE_API_KEY",
        "PYTHONPATH",
        "PYTHONHOME",
        "VIRTUAL_ENV",
    }
    environment = {
        name: value
        for name, value in os.environ.items()
        if name not in excluded and not name.startswith("VALUTATRADE_")
    }
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def check_installation(python: Path, cli: Path, parser: Path) -> None:
    """Validate package origin, resource access, both entry points, and trading."""
    environment = clean_environment()
    with TemporaryDirectory(prefix="valutatrade-installed-") as temporary:
        directory = Path(temporary)

        def run(command, *, input_text=None, expected=0, env=None):
            result = subprocess.run(
                [str(part) for part in command],
                cwd=directory,
                env=environment if env is None else env,
                input=input_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
                check=False,
            )
            if result.returncode != expected:
                raise RuntimeError(
                    f"Unexpected exit status {result.returncode}: {result.stderr}"
                )
            return result

        probe = run(
            [
                python,
                "-B",
                "-X",
                "utf8",
                "-I",
                "-c",
                (
                    "import json, sys; from pathlib import Path; "
                    "from importlib.resources import files; "
                    "from importlib.metadata import distribution; "
                    "import valutatrade_hub; "
                    "package = Path(valutatrade_hub.__file__).resolve(); "
                    "prefix = Path(sys.prefix).resolve(); "
                    "assert sys.prefix != sys.base_prefix; "
                    "assert package.is_relative_to(prefix), "
                    "'Package loaded from checkout'; "
                    "direct = distribution('finalproject-tkachuk-m26-555')"
                    ".read_text('direct_url.json'); "
                    "assert not json.loads(direct or '{}')"
                    ".get('dir_info', {}).get('editable'); "
                    "defaults = json.loads(files('valutatrade_hub')"
                    ".joinpath('default_config.json').read_text('utf-8')); "
                    "assert defaults['rates_ttl_seconds'] == 300; "
                    "print(json.dumps({'package': str(package), "
                    "'prefix': str(prefix)}))"
                ),
            ]
        )
        origin = json.loads(probe.stdout)
        package = Path(origin["package"]).parent
        before = {
            str(path.relative_to(package)): path.read_bytes()
            for path in package.rglob("*")
            if path.is_file()
        }
        for command in (
            [cli, "--help"],
            [cli, "help", "get-rate"],
            [parser, "--help"],
            [python, "-B", "-X", "utf8", "-I", "-m", "valutatrade_hub", "--help"],
            [
                python,
                "-B",
                "-X",
                "utf8",
                "-I",
                "-m",
                "valutatrade_hub.parser_service",
                "--help",
            ],
        ):
            assert "usage:" in run(command).stdout
        assert not list(directory.iterdir()), "Help created application state"

        data = directory / "данные с пробелами"
        data.mkdir()
        timestamp = datetime.now(UTC).isoformat()
        (data / "rates.json").write_text(
            json.dumps(
                {
                    "pairs": {
                        "BTC_USD": {
                            "rate": 60000,
                            "updated_at": timestamp,
                            "source": "offline-check",
                        }
                    },
                    "last_refresh": timestamp,
                }
            ),
            encoding="utf-8",
        )
        result = run(
            [cli, "--data-dir", data],
            input_text=(
                "register --username alice --password test-password\n"
                "login --username alice --password test-password\n"
                "show-portfolio\ndeposit --amount 10000\n"
                "buy --currency BTC --amount 0.05\nsell --currency BTC --amount 0.01\n"
                "show-portfolio\nget-rate --from BTC --to USD\n"
                "show-rates --currency BTC\nbuy --currency BTC --amount 100\n"
                "get-rate --from ABC --to USD\nlogout\nquit\n"
            ),
        )
        assert "Портфель пуст" in result.stdout
        assert "Покупка выполнена" in result.stdout
        assert "Продажа выполнена" in result.stdout
        assert "ИТОГО: 10,000.00 USD" in result.stdout
        assert "Недостаточно средств" in result.stderr
        assert "Неизвестная валюта" in result.stderr
        assert result.stderr.count("Ошибка:") == 2
        assert "test-password" not in result.stdout + result.stderr
        run([cli, "--data-dir", data, "show-portfolio"], expected=1)
        run([cli, "show-rates", "--top", "0"], expected=2)
        configured = {**environment, "VALUTATRADE_DATA_DIR": str(data)}
        assert "BTC_USD" in run([cli, "show-rates"], env=configured).stdout
        run(
            [parser, "--data-dir", data, "--once", "--source", "exchangerate"],
            expected=1,
        )
        assert (data / "users.json").is_file()
        assert (data / "portfolios.json").is_file()
        assert (directory / "logs/actions.log").is_file()
        after = {
            str(path.relative_to(package)): path.read_bytes()
            for path in package.rglob("*")
            if path.is_file()
        }
        assert before == after, "Application changed installed package files"
        print(f"Installed package: {origin['package']}")
        print(
            "OK: isolated imports, defaults, CLI, parser, environment and offline trade"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--parser", type=Path, required=True)
    args = parser.parse_args()
    check_installation(
        args.python.absolute(), args.cli.absolute(), args.parser.absolute()
    )


if __name__ == "__main__":
    main()
