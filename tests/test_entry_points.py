"""Smoke tests for the installed package and its CLI."""

import shutil
import subprocess
import sys
from importlib.metadata import distribution


def test_import_has_no_filesystem_side_effects(tmp_path):
    """Importing the package does not create application state."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import valutatrade_hub\n"
            "import valutatrade_hub.core.models\n"
            "import valutatrade_hub.infra.database\n"
            "print('import-ok')",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "import-ok"
    assert list(tmp_path.iterdir()) == []


def test_python_module_displays_help_outside_source_tree(tmp_path):
    """The module entry point works with another current directory."""
    result = subprocess.run(
        [sys.executable, "-m", "valutatrade_hub", "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert "usage: valutatrade" in result.stdout
    assert "учебный валютный кошелёк" in result.stdout
    assert list(tmp_path.iterdir()) == []


def test_console_script_is_installed_and_displays_help(tmp_path):
    """The package's console script resolves from the active environment."""
    entry_points = distribution("finalproject-tkachuk-m26-555").entry_points
    assert any(point.name == "valutatrade" for point in entry_points)

    executable = shutil.which("valutatrade")
    assert executable is not None

    result = subprocess.run(
        [executable, "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert "usage: valutatrade" in result.stdout
    assert list(tmp_path.iterdir()) == []
