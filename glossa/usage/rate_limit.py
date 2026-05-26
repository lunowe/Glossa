"""Sliding-window rate limiter, in-memory.

Per-tenant deque of recent request timestamps. When checking, prune old
timestamps and reject if window count >= limit. Single-process only —
multi-worker deployments will need Redis-backed coordination, deferred.
"""

import asyncio
import time
from collections import deque

WINDOW_SECONDS = 60


class RateLimiter:
    def __init__(self) -> None:
        self._buckets: dict[str, deque[float]] = {}
        self._lock = asyncio.Lock()

    async def check_and_record(self, tenant_id: str, limit: int) -> tuple[bool, int]:
        """Return ``(allowed, current_count)``.

        ``allowed`` is True iff the request stays under ``limit`` after being
        recorded. The hit is only appended if it would not exceed the cap.
        """
        now = time.monotonic()
        cutoff = now - WINDOW_SECONDS
        async with self._lock:
            bucket = self._buckets.setdefault(tenant_id, deque())
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                return False, len(bucket)
            bucket.append(now)
            return True, len(bucket)

    async def current_count(self, tenant_id: str) -> int:
        """Read-only count for the current window. Used by ``QuotaStatus``."""
        now = time.monotonic()
        cutoff = now - WINDOW_SECONDS
        async with self._lock:
            bucket = self._buckets.get(tenant_id)
            if not bucket:
                return 0
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            return len(bucket)


_RATE_LIMITER: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    global _RATE_LIMITER
    if _RATE_LIMITER is None:
        _RATE_LIMITER = RateLimiter()
    return _RATE_LIMITER


def reset_rate_limiter() -> None:
    """Clear the singleton for tests."""
    global _RATE_LIMITER
    _RATE_LIMITER = None
