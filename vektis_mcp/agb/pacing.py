"""Process-wide pacing of source request starts."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from ..settings import Settings

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


class Pacer:
    """Minimum interval between request starts, shared by every caller.

    One instance is created per process and handed to every client. The
    ``asyncio`` lock is held only while the next start time is computed and the
    waiting happens; it is never held across a whole transaction, so a slow
    response cannot block other callers beyond the pacing interval. A cancelled
    waiter releases the lock and propagates ``CancelledError``.

    ``clock`` and ``sleep`` are injectable so tests can run without real time;
    the client reuses them for its own backoff and deadline arithmetic.
    """

    def __init__(
        self,
        interval_seconds: float,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if interval_seconds < 0:
            raise ValueError(f"interval_seconds must not be negative, got {interval_seconds}")
        self.interval_seconds = float(interval_seconds)
        self.clock = clock
        self.sleep = sleep
        self._lock = asyncio.Lock()
        self._last_start: float | None = None

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> Pacer:
        """Build a pacer from the configured minimum request interval."""
        return cls(settings.request_interval_seconds, clock=clock, sleep=sleep)

    @property
    def locked(self) -> bool:
        """Whether a waiter currently holds the pacing lock."""
        return self._lock.locked()

    async def wait(self) -> None:
        """Block until the caller may start its request, then claim that slot."""
        async with self._lock:
            now = self.clock()
            last = self._last_start
            if last is not None:
                due = last + self.interval_seconds
                if due > now:
                    await self.sleep(due - now)
                    # A fake clock that does not advance on sleep must still
                    # produce a monotonically advancing schedule.
                    now = max(self.clock(), due)
            self._last_start = now
