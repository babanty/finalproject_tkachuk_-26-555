"""Sequential, interruptible scheduling with separate provider intervals."""

import signal
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from threading import TIMEOUT_MAX, Event
from time import monotonic

from valutatrade_hub.core.exceptions import ValidationError, ValutaTradeError
from valutatrade_hub.core.utils import validate_amount
from valutatrade_hub.parser_service.updater import SOURCE_NAMES, UpdateResult


class RateScheduler:
    """Wait after each attempt, including failures, without catch-up bursts."""

    def __init__(
        self,
        update: Callable[[str], UpdateResult],
        intervals: Mapping[str, float],
        *,
        stop: Event | None = None,
        clock: Callable[[], float] = monotonic,
        on_result: Callable[[UpdateResult], object] | None = None,
        on_error: Callable[[ValutaTradeError], object] | None = None,
    ) -> None:
        if not intervals or intervals.keys() - SOURCE_NAMES.keys():
            raise ValidationError("Некорректные источники расписания")
        self._intervals = {
            source: validate_amount(interval) for source, interval in intervals.items()
        }
        self._update = update
        self._stop = stop if stop is not None else Event()
        self._clock = clock
        self._on_result = on_result
        self._on_error = on_error

    def run(self) -> None:
        """Run until signalled; every source is initially due immediately."""
        due = dict.fromkeys(self._intervals, self._clock())
        while not self._stop.is_set():
            for source, interval in self._intervals.items():
                if self._stop.is_set():
                    return
                if self._clock() < due[source]:
                    continue
                try:
                    result = self._update(source)
                except ValutaTradeError as error:
                    if self._on_error is not None:
                        self._on_error(error)
                else:
                    if self._on_result is not None:
                        self._on_result(result)
                due[source] = self._clock() + interval
            delay = max(0.0, min(due.values()) - self._clock())
            self._stop.wait(min(delay, TIMEOUT_MAX))


@contextmanager
def stop_on_signals(stop: Event):
    """Restore previous handlers even when setup or a scheduled update fails."""
    previous = {}

    def request_stop(signum, frame):
        stop.set()

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, request_stop)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
