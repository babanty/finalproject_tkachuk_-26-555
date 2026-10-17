"""Configuration precedence, validation, and singleton behavior."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from valutatrade_hub.core.exceptions import ValidationError
from valutatrade_hub.infra.settings import SettingsLoader


def test_packaged_defaults_work_without_source_config_and_create_no_files(
    tmp_path, monkeypatch
):
    """Defaults are package resources rather than a cwd pyproject lookup."""
    monkeypatch.chdir(tmp_path)
    settings = SettingsLoader()
    assert settings.get("rates_ttl_seconds") == 300
    assert settings.get("data_dir") == tmp_path / "data"
    assert settings.get("log_dir") == tmp_path / "logs"
    assert settings.get("users_file") == tmp_path / "data" / "users.json"
    assert settings.get("unknown", "fallback") == "fallback"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "use_file,use_environment,use_override,expected",
    [
        (False, False, False, 300),
        (True, False, False, 120),
        (True, True, False, 60),
        (True, True, True, 30),
    ],
)
def test_configuration_precedence(
    tmp_path, monkeypatch, use_file, use_environment, use_override, expected
):
    """Explicit options override env, which overrides JSON and defaults."""
    config = tmp_path / "config.json"
    config.write_text('{"rates_ttl_seconds":120}', encoding="utf-8")
    if use_environment:
        monkeypatch.setenv("VALUTATRADE_RATES_TTL_SECONDS", "60")
    settings = SettingsLoader(
        config if use_file else None,
        overrides={"rates_ttl_seconds": 30} if use_override else None,
    )
    assert settings.get("rates_ttl_seconds") == expected


def test_paths_follow_overridden_data_directory(tmp_path, monkeypatch):
    """All derived paths track the chosen data root, including Unicode."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VALUTATRADE_DATA_DIR", "каталог с пробелами/data")
    settings = SettingsLoader()
    assert settings.get("data_dir") == tmp_path / "каталог с пробелами" / "data"
    assert settings.get("log_dir") == tmp_path / "каталог с пробелами" / "logs"
    assert settings.get("history_file").parent == settings.get("data_dir")
    assert not settings.get("data_dir").exists()


def test_explicit_log_directory_overrides_default(tmp_path, monkeypatch):
    """A supplied log location is independent of the data directory."""
    monkeypatch.chdir(tmp_path)
    settings = SettingsLoader(overrides={"log_dir": "other logs"})
    assert settings.get("log_dir") == tmp_path / "other logs"


def test_repeated_construction_keeps_cached_configuration(monkeypatch):
    """Singleton construction does not reload newly changed environment values."""
    settings = SettingsLoader(overrides={"rates_ttl_seconds": 42})
    monkeypatch.setenv("VALUTATRADE_RATES_TTL_SECONDS", "7")
    assert SettingsLoader() is settings
    assert SettingsLoader(overrides={"rates_ttl_seconds": 42}) is settings
    assert settings.get("rates_ttl_seconds") == 42
    with pytest.raises(ValidationError, match="другой конфигурацией"):
        SettingsLoader(overrides={"rates_ttl_seconds": 99})
    assert settings.get("rates_ttl_seconds") == 42


@pytest.mark.parametrize(
    "override",
    [
        {"rates_ttl_seconds": 0},
        {"rates_ttl_seconds": 1.5},
        {"rates_ttl_seconds": True},
        {"rates_ttl_seconds": "abc"},
        {"update_interval_seconds": -1},
        {"log_max_bytes": 0},
        {"log_backup_count": False},
        {"request_timeout_seconds": "nan"},
        {"request_timeout_seconds": float("inf")},
        {"lock_timeout_seconds": 0},
        {"lock_timeout_seconds": None},
        {"log_format": "yaml"},
        {"log_level": "VERBOSE"},
        {"log_level": 10},
        {"data_dir": ""},
        {"data_dir": None},
        {"log_dir": []},
        {"unknown_option": 1},
    ],
)
def test_invalid_configuration_does_not_silently_fall_back(override):
    """Configuration errors must be visible instead of changing behavior silently."""
    with pytest.raises(ValidationError):
        SettingsLoader(overrides=override)


@pytest.mark.parametrize("contents", ["[]", "{bad", '{"rates_ttl_seconds":NaN}'])
def test_malformed_config_is_rejected(tmp_path, contents):
    """Invalid JSON and wrong top-level types produce configuration errors."""
    path = tmp_path / "config.json"
    path.write_text(contents, encoding="utf-8")
    with pytest.raises(ValidationError):
        SettingsLoader(path)


def test_missing_config_is_an_error_and_failed_init_can_be_retried(tmp_path):
    """A failed first initialization does not poison the cached singleton."""
    path = tmp_path / "config.json"
    with pytest.raises(ValidationError, match="загрузить"):
        SettingsLoader(path)
    path.write_text(json.dumps({"base_currency": "eur"}), encoding="utf-8")
    settings = SettingsLoader(path)
    assert settings.get("base_currency") == "EUR"


def test_singleton_fixture_resets_configuration_between_cases():
    """This case observes defaults rather than another test's explicit values."""
    assert SettingsLoader().get("rates_ttl_seconds") == 300


def test_concurrent_construction_keeps_one_settings_instance():
    """Concurrent constructors cannot initialize separate cached configurations."""
    with ThreadPoolExecutor(max_workers=8) as executor:
        instances = list(executor.map(lambda index: SettingsLoader(), range(24)))
    assert all(instance is instances[0] for instance in instances)
    assert instances[0].get("rates_ttl_seconds") == 300
