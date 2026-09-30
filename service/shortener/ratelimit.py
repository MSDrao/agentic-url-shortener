"""In-process token-bucket rate limiter.

Per-instance only: with N replicas the effective limit is N x the configured
rate. A shared store (e.g. Redis) is the production upgrade path.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable


class TokenBucketLimiter:
    def __init__(
        self,
        rate_per_minute: int,
        burst: int | None = None,
        max_keys: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ):
        if rate_per_minute <= 0:
            raise ValueError("rate_per_minute must be positive")
        self.rate = rate_per_minute / 60.0
        self.capacity = float(burst or rate_per_minute)
        self.max_keys = max_keys
        self.clock = clock
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._lock = threading.Lock()

    def allow(self, key: str) -> tuple[bool, float]:
        """Return (allowed, retry_after_seconds)."""
        now = self.clock()
        with self._lock:
            tokens, last = self._buckets.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            if tokens >= 1.0:
                self._buckets[key] = (tokens - 1.0, now)
                allowed, retry_after = True, 0.0
            else:
                self._buckets[key] = (tokens, now)
                allowed, retry_after = False, (1.0 - tokens) / self.rate
            self._buckets.move_to_end(key)
            while len(self._buckets) > self.max_keys:  # bound memory (LRU eviction)
                self._buckets.popitem(last=False)
        return allowed, retry_after
