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


def classify_api_content(
    body_text: str,
    json_field_path: str,
    keyword: str,
    *,
    status_code: int | None = None,
    latency_ms: int | None = None,
) -> Result:
    """Classify an API-content Check: parse JSON, read a field by dot-path, and
    fail if the field's value contains the alarm keyword (substring match).

    Pure function of (body, path, keyword) → Result:
    - Non-JSON body → failure with reason.
    - Missing field or null → failure with reason.
    - Field value contains keyword → failure (the alarm condition).
    - Otherwise → success.

    Example: body ``{"mode": "stale"}``, path ``mode``, keyword ``stale`` → fail.
    """
    import json

    try:
        data = json.loads(body_text)
    except (json.JSONDecodeError, TypeError):
        return Result(
            success=False,
            reason="response is not valid JSON",
            status_code=status_code,
            latency_ms=latency_ms,
        )

    # Walk the dot-notation path through nested dicts.
    current: object = data
    parts = json_field_path.split(".")
    walked: list[str] = []
    for part in parts:
        walked.append(part)
        if not isinstance(current, dict) or part not in current:
            return Result(
                success=False,
                reason=f"field '{'.'.join(walked)}' not found in response",
                status_code=status_code,
                latency_ms=latency_ms,
            )
        current = current[part]

    if current is None:
        return Result(
            success=False,
            reason=f"field '{json_field_path}' is null",
            status_code=status_code,
            latency_ms=latency_ms,
        )

    value_str = str(current)
    if keyword in value_str:
        return Result(
            success=False,
            reason=f"field '{json_field_path}' contains '{keyword}' (value: {value_str[:80]})",
            status_code=status_code,
            latency_ms=latency_ms,
        )

    return Result(
        success=True,
        reason=f"field '{json_field_path}' = {value_str[:80]}",
        status_code=status_code,
        latency_ms=latency_ms,
    )
