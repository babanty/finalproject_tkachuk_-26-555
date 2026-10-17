"""Isolate application singletons and configuration between tests."""

import os

import pytest
import requests

from valutatrade_hub.core.models import Portfolio, User, Wallet
from valutatrade_hub.infra.database import DatabaseManager
from valutatrade_hub.infra.settings import SettingsLoader
from valutatrade_hub.logging_config import shutdown_logging


@pytest.fixture(autouse=True)
def isolate_configuration(monkeypatch):
    """Prevent the host environment and earlier instances affecting another case."""
    monkeypatch.setattr(SettingsLoader, "_instance", None)
    monkeypatch.setattr(DatabaseManager, "_instance", None)
    for key in list(os.environ):
        if key.startswith("VALUTATRADE_") or key in {
            "COINGECKO_API_KEY",
            "EXCHANGERATE_API_KEY",
        }:
            monkeypatch.delenv(key)
    shutdown_logging()
    yield
    shutdown_logging()


@pytest.fixture(autouse=True)
def block_http_requests(monkeypatch):
    """All tests must inject a transport instead of contacting a live API."""

    def forbidden_request(*args, **kwargs):
        raise AssertionError("Реальные HTTP-запросы запрещены в тестах")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden_request)


@pytest.fixture
def database(tmp_path):
    """Create an initialized repository in an isolated Unicode path."""
    settings = SettingsLoader(overrides={"data_dir": tmp_path / "кошельки с пробелами"})
    repository = DatabaseManager(settings)
    repository.initialize()
    return repository


@pytest.fixture
def saved_account(database):
    """Persist a funded educational account for repository tests."""
    user = User.create(1, "Алиса", "secret")
    portfolio = Portfolio(
        1,
        {"USD": Wallet("USD", 10)},
        user=user,
        rate_provider=lambda from_code, to_code: 1.0,
    )
    database.save_account(user, portfolio)
    return user, portfolio
