"""A global token bucket in front of every PikPak request.

PikPak has risk controls, and bulk operations such as renaming hundreds of
files are what trip them. Every request waits for a token; the bucket refills
at ``rate`` tokens a second and holds at most ``burst``, so short bursts are
allowed and the long-run rate never exceeds ``rate``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable


class TokenBucket:
    def __init__(
        self,
        rate: float,
        burst: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if rate <= 0 or burst < 1:
            raise ValueError("rate must be positive and burst at least 1")
        self._rate = rate
        self._burst = float(burst)
        self._tokens = float(burst)
        self._clock = clock
        self._sleep = sleep
        self._updated = clock()
        # One waiter at a time, so tokens are handed out in arrival order.
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self._burst, self._tokens + (now - self._updated) * self._rate)
        self._updated = now

    async def acquire(self) -> None:
        async with self._lock:
            self._refill()
            while self._tokens < 1:
                await self._sleep((1 - self._tokens) / self._rate)
                self._refill()
            self._tokens -= 1
