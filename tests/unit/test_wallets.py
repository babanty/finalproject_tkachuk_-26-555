"""Balance validation and atomic in-memory wallet updates."""

import math
import sys

import pytest

from valutatrade_hub.core.exceptions import (
    CurrencyNotFoundError,
    InsufficientFundsError,
    ValidationError,
)
from valutatrade_hub.core.models import Wallet

INVALID_AMOUNTS = [
    0,
    -1,
    True,
    False,
    "1",
    None,
    float("nan"),
    float("inf"),
    float("-inf"),
    1 + 2j,
    10**400,
]


def test_wallet_defaults_and_positive_deposit():
    """Int and float deposits update a normalized wallet without rounding."""
    wallet = Wallet("btc")
    assert wallet.balance == 0.0
    assert isinstance(wallet.balance, float)
    assert wallet.currency_code == "BTC"
    wallet.deposit(1)
    wallet.deposit(0.00000001)
    assert wallet.balance == pytest.approx(1.00000001)


@pytest.mark.parametrize("operation", ["deposit", "withdraw"])
@pytest.mark.parametrize("amount", INVALID_AMOUNTS)
def test_rejected_amount_leaves_wallet_unchanged(operation, amount):
    """Invalid amounts are rejected before either operation changes state."""
    wallet = Wallet("USD", 10)
    with pytest.raises(ValidationError, match="amount"):
        getattr(wallet, operation)(amount)
    assert wallet.balance == 10


@pytest.mark.parametrize("balance", INVALID_AMOUNTS[1:])
def test_balance_setter_and_constructor_reject_invalid_values(balance):
    """Invalid persisted balances and assignments cannot enter a wallet."""
    wallet = Wallet("USD", 10)
    with pytest.raises(ValidationError, match="Баланс"):
        wallet.balance = balance
    assert wallet.balance == 10
    with pytest.raises(ValidationError, match="Баланс"):
        Wallet("USD", balance)


def test_balance_setter_accepts_zero_and_positive_values():
    """The balance property permits zero while amount validation does not."""
    wallet = Wallet("EUR", 1)
    wallet.balance = 0
    assert wallet.balance == 0.0
    wallet.balance = 2.5
    assert wallet.balance == 2.5


def test_withdraw_partial_and_entire_balance():
    """A wallet can withdraw part of its funds and then its exact remainder."""
    wallet = Wallet("USD", 10)
    wallet.withdraw(4)
    assert wallet.balance == 6
    wallet.withdraw(6)
    assert wallet.balance == 0


def test_insufficient_funds_exposes_context_and_preserves_balance():
    """The caller receives a readable domain error without a partial debit."""
    wallet = Wallet("BTC", 1)
    with pytest.raises(InsufficientFundsError) as captured:
        wallet.withdraw(2)
    assert wallet.balance == 1
    assert captured.value.available == 1
    assert captured.value.required == 2
    assert captured.value.code == "BTC"
    assert str(captured.value) == (
        "Недостаточно средств: доступно 1 BTC, требуется 2 BTC"
    )


def test_even_smallest_representable_overdraft_is_rejected():
    """Floating-point tolerances must not grant extra spending power."""
    wallet = Wallet("USD", 1)
    with pytest.raises(InsufficientFundsError):
        wallet.withdraw(math.nextafter(1.0, math.inf))
    assert wallet.balance == 1


def test_deposit_overflow_does_not_replace_balance():
    """Finite inputs cannot cause the saved balance to become infinity."""
    wallet = Wallet("USD", sys.float_info.max)
    with pytest.raises(ValidationError, match="Баланс"):
        wallet.deposit(sys.float_info.max)
    assert wallet.balance == sys.float_info.max


def test_balance_info_keeps_tiny_values_visible():
    """The display contains the actual small quantity and currency."""
    wallet = Wallet("BTC", 0.000000001)
    assert wallet.get_balance_info() == "BTC: 1e-09"
    assert wallet.balance == 0.000000001


def test_wallet_currency_is_known_and_cannot_be_reassigned():
    """Changing a code must not reinterpret an existing balance."""
    with pytest.raises(CurrencyNotFoundError):
        Wallet("ABC")
    wallet = Wallet("USD", 1)
    with pytest.raises(AttributeError):
        wallet.currency_code = "EUR"
