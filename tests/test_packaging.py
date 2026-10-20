"""Keep the source archive reproducible without bundling runtime or credentials."""

import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_source_distribution_includes_verification_inputs():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    backend = config["tool"]["uv"]["build-backend"]
    assert backend["module-name"] == "valutatrade_hub"
    assert backend["module-root"] == ""
    assert {
        ".python-version",
        ".env.example",
        "uv.lock",
        "main.py",
        "scripts/**",
        "tests/**",
        "data/examples/**",
    } <= set(backend["source-include"])
    assert {".env", "docs/**", "logs/**", "data/*.json"} <= set(
        backend["source-exclude"]
    )


def test_environment_example_contains_only_empty_credentials():
    values = dict(
        line.split("=", 1)
        for line in (ROOT / ".env.example").read_text("utf-8").splitlines()
        if line.strip()
    )
    assert values == {"COINGECKO_API_KEY": "", "EXCHANGERATE_API_KEY": ""}


def test_installation_check_help_does_not_create_files(tmp_path):
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/check_installed.py"), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--python" in result.stdout and "--parser" in result.stdout
    assert not list(tmp_path.iterdir())
