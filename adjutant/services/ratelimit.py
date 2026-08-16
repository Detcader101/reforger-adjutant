"""A simple per-key token-bucket rate limiter.

Pure logic: no discord imports, no wall-clock reads baked in (the clock is
injectable), so it's testable without sleeping. Cogs key it by
(guild_id, user_id, command_name) to get per-user-per-command limits.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Hashable
from dataclasses import dataclass


@dataclass(slots=True)
class _Bucket:
    tokens: float
    last_check: float


class TokenBucketLimiter:
    """Allows up to `capacity` calls in a burst, then refills one token
    every `refill_seconds`, per key."""

    def __init__(
        self,
        capacity: int,
        refill_seconds: float,
        *,
        time_fn: Callable[[], float] = time.monotonic,
    ):
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        if refill_seconds <= 0:
            raise ValueError("refill_seconds must be positive")
        self.capacity = capacity
        self.refill_seconds = refill_seconds
        self._time_fn = time_fn
        self._buckets: dict[Hashable, _Bucket] = {}

    def allow(self, key: Hashable) -> bool:
        """Consume a token for `key` if one is available. Returns whether
        the call is allowed."""
        now = self._time_fn()
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(tokens=float(self.capacity), last_check=now)
            self._buckets[key] = bucket
        else:
            elapsed = now - bucket.last_check
            bucket.tokens = min(self.capacity, bucket.tokens + elapsed / self.refill_seconds)
            bucket.last_check = now

        if bucket.tokens >= 1:
            bucket.tokens -= 1
            return True
        return False
