"""Parser defaults remain independent and credentials are never represented."""

import pytest

from valutatrade_hub.core.exceptions import ValidationError
from valutatrade_hub.infra.settings import SettingsLoader
from valutatrade_hub.parser_service.config import CRYPTO_ID_MAP, ParserConfig


def test_environment_is_read_at_construction_and_keys_are_not_in_repr(monkeypatch):
    before = ParserConfig()
    monkeypatch.setenv("EXCHANGERATE_API_KEY", "private-exchange-key")
    monkeypatch.setenv("COINGECKO_API_KEY", "private-gecko-key")
    after = ParserConfig()
    assert before.exchangerate_api_key is before.coingecko_api_key is None
    assert after.exchangerate_api_key == "private-exchange-key"
    assert after.coingecko_api_key == "private-gecko-key"
    assert "private" not in repr(after)


def test_explicit_credentials_override_environment(monkeypatch):
    monkeypatch.setenv("EXCHANGERATE_API_KEY", "environment-key")
    config = ParserConfig(exchangerate_api_key="explicit-key", coingecko_api_key=None)
    assert config.exchangerate_api_key == "explicit-key"
    assert config.coingecko_api_key is None


def test_dictionary_defaults_and_input_are_not_shared():
    first, second = ParserConfig(), ParserConfig()
    first.crypto_id_map["BTC"] = "changed"
    assert second.crypto_id_map == CRYPTO_ID_MAP
    custom = CRYPTO_ID_MAP.copy()
    config = ParserConfig(crypto_id_map=custom)
    custom.clear()
    assert config.crypto_id_map == CRYPTO_ID_MAP


def test_settings_bridge_preserves_paths_timeout_and_has_no_file_side_effects(tmp_path):
    settings = SettingsLoader(
        overrides={"data_dir": tmp_path / "данные", "request_timeout_seconds": 7}
    )
    config = ParserConfig.from_settings(settings)
    assert config.rates_file == settings.get("rates_file")
    assert config.history_file == settings.get("history_file")
    assert config.request_timeout_seconds == 7
    assert list(tmp_path.iterdir()) == []


def test_currency_lists_are_normalized_and_can_select_a_subset():
    config = ParserConfig(fiat_currencies=["eur"], crypto_currencies=["btc"])
    assert config.fiat_currencies == ("EUR",)
    assert config.crypto_currencies == ("BTC",)


@pytest.mark.parametrize(
    "override",
    [
        {"base_currency": "EUR"},
        {"fiat_currencies": []},
        {"fiat_currencies": "EUR"},
        {"fiat_currencies": ["BTC"]},
        {"fiat_currencies": ["USD"]},
        {"fiat_currencies": ["EUR", "eur"]},
        {"crypto_currencies": ["EUR"]},
        {"crypto_currencies": []},
        {"crypto_id_map": {}},
        {"crypto_id_map": []},
        {"crypto_id_map": {"BTC": "bad/id"}},
        {"request_timeout_seconds": 0},
        {"request_timeout_seconds": True},
        {"request_timeout_seconds": float("nan")},
        {"coingecko_api_key": "private key"},
        {"exchangerate_api_key": "private\r\nheader"},
        {"exchangerate_api_key": 42},
        {"coingecko_url": "http://example.test"},
        {"coingecko_url": "https://user:private@example.test/path"},
        {"exchangerate_api_url": "https://example.test?key=private"},
        {"exchangerate_api_url": None},
        {"rates_file": None},
    ],
)
def test_invalid_configuration_is_rejected_without_echoing_values(override):
    with pytest.raises(ValidationError) as captured:
        ParserConfig(**override)
    assert "private" not in str(captured.value)
