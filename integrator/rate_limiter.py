"""Sliding-window rate limiter for outbound API calls.

In-process (single worker). For multi-worker scale-out switch to a Redis-backed
token bucket — the interface here is unchanged. See AGENTS.md.
"""
import threading
import time
from collections import deque
from typing import Callable


class RateLimiter:
    def __init__(
        self,
        rate: int,
        per: float = 1.0,
        sleep_fn: Callable[[float], None] = time.sleep,
        monotonic_fn: Callable[[], float] = time.monotonic,
    ):
        if rate < 1:
            raise ValueError("rate must be >= 1")
        self.rate = rate
        self.per = per
        self._sleep = sleep_fn
        self._monotonic = monotonic_fn
        self._timestamps: deque[float] = deque(maxlen=rate)
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Block (sleep) until a request slot is free."""
        with self._lock:
            now = self._monotonic()
            if len(self._timestamps) < self.rate:
                self._timestamps.append(now)
                return
            oldest = self._timestamps[0]
            wait = self.per - (now - oldest)
            if wait > 0:
                self._sleep(wait)
                now += wait
            self._timestamps.append(now)
