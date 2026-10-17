"""Reusable domain fixtures with deterministic registration times and quotes."""

from datetime import UTC, datetime

import pytest

from valutatrade_hub.core.models import User


@pytest.fixture
def user():
    """Create an isolated user without files or external services."""
    return User.create(1, "alice", "secret", datetime(2026, 10, 17, tzinfo=UTC))


@pytest.fixture
def rate_provider():
    """Provide fixed educational rates for valuation tests."""
    prices = {"USD": 1.0, "EUR": 1.25, "BTC": 60000.0}

    def get_rate(from_code, to_code):
        """Quote one supported fixture currency in another."""
        return prices[from_code] / prices[to_code]

    return get_rate
