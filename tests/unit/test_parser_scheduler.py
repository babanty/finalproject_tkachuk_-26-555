"""Deterministic scheduling with no real waits or network access."""

import signal
from threading import TIMEOUT_MAX, Event
from unittest.mock import Mock

import pytest

from valutatrade_hub.core.exceptions import (
    ApiRequestError,
    StorageError,
    ValidationError,
)
from valutatrade_hub.parser_service.scheduler import RateScheduler, stop_on_signals
from valutatrade_hub.parser_service.updater import UpdateResult


class FakeTime:
    def __init__(self, end=1000):
        self.now = 0.0
        self.end = end
        self.stopped = False
        self.waits = []

    def clock(self):
        return self.now

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, delay):
        assert delay > 0
        self.waits.append(delay)
        self.now += delay
        if self.now >= self.end:
            self.set()
        return self.is_set()


def test_separate_intervals_no_overlap_or_catchup():
    time = FakeTime(end=91000)
    calls = []

    def update(source):
        calls.append((source, time.now))
        time.now += 20
        return UpdateResult((source,), {}, 3, None)

    results = Mock()
    RateScheduler(
        update,
        {"coingecko": 300, "exchangerate": 86400},
        stop=time,
        clock=time.clock,
        on_result=results,
    ).run()
    fiat = [stamp for source, stamp in calls if source == "exchangerate"]
    crypto = [stamp for source, stamp in calls if source == "coingecko"]
    assert len(fiat) == 2
    assert fiat[1] - fiat[0] >= 86420
    assert all(after - before >= 320 for before, after in zip(crypto, crypto[1:]))
    assert results.call_count == len(calls)


@pytest.mark.parametrize("error", [ApiRequestError("offline"), StorageError("disk")])
def test_expected_failure_waits_and_continues(error):
    time = FakeTime(end=650)
    success = UpdateResult(("coingecko",), {}, 1, None)
    update = Mock(side_effect=[error, success, success])
    errors, results = Mock(), Mock()
    RateScheduler(
        update,
        {"coingecko": 300},
        stop=time,
        clock=time.clock,
        on_error=errors,
        on_result=results,
    ).run()
    assert update.call_count == 3
    errors.assert_called_once_with(error)
    assert results.call_count == 2
    assert time.waits == [300, 300, 300]


def test_failed_result_is_reported_and_does_not_spin():
    time = FakeTime(end=500)
    failed = UpdateResult((), {"coingecko": "offline"}, 0, None)
    update = Mock(return_value=failed)
    results = Mock()
    RateScheduler(
        update,
        {"coingecko": 300},
        stop=time,
        clock=time.clock,
        on_result=results,
    ).run()
    assert results.call_count == update.call_count == 2
    assert time.waits == [300, 300]


def test_stop_during_update_skips_other_provider():
    time = FakeTime()
    update = Mock(side_effect=lambda source: time.set())
    RateScheduler(
        update,
        {"coingecko": 300, "exchangerate": 86400},
        stop=time,
    ).run()
    update.assert_called_once_with("coingecko")
    assert not time.waits


def test_preexisting_stop_and_unexpected_error():
    stop = Event()
    stop.set()
    update = Mock(side_effect=RuntimeError("bug"))
    RateScheduler(update, {"coingecko": 300}, stop=stop).run()
    update.assert_not_called()
    stop.clear()
    with pytest.raises(RuntimeError):
        RateScheduler(update, {"coingecko": 300}, stop=stop).run()


def test_long_wait_is_bounded_for_platform_event():
    time = FakeTime(end=1)
    RateScheduler(Mock(), {"coingecko": 1e100}, stop=time, clock=time.clock).run()
    assert time.waits == [TIMEOUT_MAX]


@pytest.mark.parametrize(
    "intervals",
    [
        {},
        {"bad": 5},
        {"coingecko": 0},
        {"exchangerate": -1},
        {"coingecko": float("nan")},
    ],
)
def test_invalid_schedule(intervals):
    with pytest.raises(ValidationError):
        RateScheduler(Mock(), intervals)


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_signal_stops_wait_and_restores_handlers(signum):
    previous = {
        number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)
    }
    stop = Event()
    with pytest.raises(RuntimeError), stop_on_signals(stop):
        signal.getsignal(signum)(signum, None)
        assert stop.wait(0)
        raise RuntimeError("exit")
    assert all(
        signal.getsignal(number) == handler for number, handler in previous.items()
    )
