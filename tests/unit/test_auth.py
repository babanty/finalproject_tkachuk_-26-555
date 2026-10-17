"""Account operations preserve storage invariants and isolate sessions."""

from dataclasses import asdict

import pytest

from valutatrade_hub.core.exceptions import (
    AuthenticationError,
    CurrencyNotFoundError,
    StorageError,
    ValidationError,
)
from valutatrade_hub.core.models import Portfolio, User
from valutatrade_hub.core.usecases import TradingService
from valutatrade_hub.infra import database as database_module


def no_rates(from_code, to_code):
    raise AssertionError("Account operations must not request exchange rates")


@pytest.fixture
def service(database):
    return TradingService(database, rate_provider=no_rates)


def account_bytes(database):
    return tuple(
        database.get_path(name).read_bytes() for name in ("users", "portfolios")
    )


def test_registration_persists_empty_portfolio_without_login(service, database):
    result = service.register("  Алиса  ", " secret ")
    assert asdict(result) == {"user_id": 1, "username": "Алиса"}
    assert service.current_user is None
    saved = database.get_user(result.user_id)
    assert saved.verify_password(" secret ")
    assert not saved.verify_password("secret")
    assert b" secret " not in account_bytes(database)[0]
    portfolio = database.get_portfolio(result.user_id, rate_provider=no_rates)
    assert portfolio.wallets == {}
    assert portfolio.get_total_value() == 0


def test_registration_uses_maximum_id_and_case_sensitive_names(service, database):
    owner = User.create(7, "Алиса", "secret")
    database.save_account(owner, Portfolio(7, user=owner, rate_provider=no_rates))
    assert service.register("алиса", "secret").user_id == 8


@pytest.mark.parametrize(
    ("username", "password", "message"),
    [
        ("Алиса", "other", "уже занято"),
        ("Боб", "123", "4 символов"),
        ("   ", "secret", "Имя пользователя"),
    ],
)
def test_invalid_registration_does_not_change_files(
    service, database, username, password, message
):
    service.register("Алиса", "secret")
    before = account_bytes(database)
    with pytest.raises(ValidationError, match=message):
        service.register(username, password)
    assert account_bytes(database) == before


def test_second_write_failure_rolls_back_registration(service, database, monkeypatch):
    service.register("Алиса", "secret")
    before = account_bytes(database)
    original = database_module.atomic_write_json

    def fail_portfolio(path, data):
        if path == database.get_path("portfolios"):
            raise StorageError("write failed")
        original(path, data)

    monkeypatch.setattr(database_module, "atomic_write_json", fail_portfolio)
    with pytest.raises(StorageError, match="восстановлены"):
        service.register("Боб", "secret")
    assert account_bytes(database) == before
    assert service.current_user is None


def test_login_logout_and_registration_keep_sessions_explicit(service, database):
    first = service.register("Алиса", "secret")
    assert service.login(" Алиса ", "secret") == first
    second = service.register("Боб", "other")
    assert service.current_user == first
    assert service.login("Боб", "other") == second
    assert TradingService(database, rate_provider=no_rates).current_user is None
    service.logout()
    service.logout()
    assert service.current_user is None


@pytest.mark.parametrize(
    ("username", "password", "error_type", "message"),
    [
        ("Боб", "secret", AuthenticationError, "не найден"),
        ("Алиса", "wrong", AuthenticationError, "Неверный пароль"),
        ("", "secret", ValidationError, "Имя пользователя"),
    ],
)
def test_failed_login_discards_previous_session(
    service, database, username, password, error_type, message
):
    service.register("Алиса", "secret")
    service.login("Алиса", "secret")
    before = account_bytes(database)
    with pytest.raises(error_type, match=message):
        service.login(username, password)
    assert service.current_user is None
    assert account_bytes(database) == before
    with pytest.raises(AuthenticationError, match="Сначала выполните login"):
        service.show_portfolio()


def test_empty_portfolio_validates_base_without_requesting_rates(service):
    service.register("Алиса", "secret")
    with pytest.raises(AuthenticationError):
        service.show_portfolio()
    service.login("Алиса", "secret")
    view = service.show_portfolio("eur")
    assert (view.username, view.base_currency, view.balances, view.total) == (
        "Алиса",
        "EUR",
        (),
        0,
    )
    with pytest.raises(CurrencyNotFoundError):
        service.show_portfolio("ABC")


def test_portfolio_is_loaded_afresh(service, database, saved_account):
    service.login("Алиса", "secret")
    assert service.show_portfolio().total == 10
    database.update_portfolio(
        1,
        lambda portfolio: portfolio.get_wallet("USD").deposit(5),
        rate_provider=no_rates,
    )
    assert service.show_portfolio().balances == (("USD", 15),)
    assert service.show_portfolio().total == 15
