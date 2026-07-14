"""The Result of a single Check.

A Result is the outcome of one Check execution: success/failure, latency,
status code (when applicable), and a human-readable reason. This module holds
the **pure** Result type and classification helpers — no DB, no network.

Per CONTEXT.md:
    Result — The outcome of one Check: success/failure, latency, status code,
    and a human-readable reason. Stored as an append-only time series per
    Monitor.

Keeping Result pure (a frozen dataclass) means the executor seam
(:mod:`tgmonitor.executors`) and its tests never touch the ORM.
"""

from __future__ import annotations

from dataclasses import dataclass

# A standalone result row id is allocated by the DB (hypertable identity); the
# pure Result carries only what a Check produces. ``checked_at`` is injected by
# the executor wrapper so tests can pin time.


@dataclass(frozen=True, slots=True)
class Result:
    """Outcome of a single Check execution."""

    success: bool
    reason: str
    latency_ms: int | None = None
    status_code: int | None = None
    checked_at: float | None = None  # POSIX timestamp; set by the engine wrapper


def classify_http(
    status_code: int,
    *,
    expected_status: int = 200,
    body_contains: str | None = None,
    body_text: str = "",
    latency_ms: int | None = None,
    max_latency_ms: int | None = None,
) -> Result:
    """Classify an HTTP response into a :class:`Result`.

    Pure function of (status, body, latency) → Result. This is the heart of the
    HTTP executor and is unit-tested independently of any transport.
    """
    if status_code != expected_status:
        return Result(
            success=False,
            reason=f"HTTP {status_code} (expected {expected_status})",
            status_code=status_code,
            latency_ms=latency_ms,
        )
    if body_contains is not None and body_contains not in body_text:
        return Result(
            success=False,
            reason=f"body missing keyword {body_contains!r}",
            status_code=status_code,
            latency_ms=latency_ms,
        )
    if max_latency_ms is not None and latency_ms is not None and latency_ms > max_latency_ms:
        return Result(
            success=False,
            reason=f"latency {latency_ms}ms exceeds {max_latency_ms}ms",
            status_code=status_code,
            latency_ms=latency_ms,
        )
    return Result(
        success=True,
        reason="ok",
        status_code=status_code,
        latency_ms=latency_ms,
    )
