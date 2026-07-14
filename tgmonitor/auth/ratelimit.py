"""In-memory sliding-window rate limiter for auth endpoints.

Abuse prevention (ADR-0011), not a distributed guarantee. Sufficient for the
single-VPS launch envelope; the limiter state is per-process. Keyed by either
an IP address or an email (two independent windows).

The window is sliding: each key keeps a deque of timestamps, pruned to the
configured window. A check appends ``now`` and fails if the count would exceed
the limit. No persistence — restarts reset the counters, which is acceptable
for throttling brute force.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from datetime import UTC, datetime

from tgmonitor.config import get_settings


class RateLimiter:
    """Sliding-window counter. Thread-safe-ish via a single asyncio lock.

    Call :meth:`check` on each auth attempt; it returns True (allowed) and
    records the attempt, or False (rate-limited) and does not record.
    """

    def __init__(
        self,
        window_s: int | None = None,
        max_per_key: int | None = None,
    ) -> None:
        settings = get_settings()
        self._window_s = window_s if window_s is not None else settings.auth_rate_window_s
        self._max = max_per_key if max_per_key is not None else settings.auth_rate_max_per_ip
        self._hits: dict[str, deque[datetime]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def check(self, key: str) -> bool:
        """Record an attempt for ``key``; return True if within the limit."""
        now = datetime.now(UTC)
        cutoff = datetime.fromtimestamp(now.timestamp() - self._window_s, tz=UTC)
        async with self._lock:
            bucket = self._hits[key]
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self._max:
                return False
            bucket.append(now)
            return True


# Module-level limiters — one per dimension (IP, email), each with its own cap.
_ip_limiter: RateLimiter | None = None
_email_limiter: RateLimiter | None = None


def get_ip_limiter() -> RateLimiter:
    global _ip_limiter
    if _ip_limiter is None:
        s = get_settings()
        _ip_limiter = RateLimiter(window_s=s.auth_rate_window_s, max_per_key=s.auth_rate_max_per_ip)
    return _ip_limiter


def get_email_limiter() -> RateLimiter:
    global _email_limiter
    if _email_limiter is None:
        s = get_settings()
        _email_limiter = RateLimiter(
            window_s=s.auth_rate_window_s, max_per_key=s.auth_rate_max_per_email
        )
    return _email_limiter
