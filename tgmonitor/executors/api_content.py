"""API-content Check executor.

GETs a URL that returns JSON, reads a configured field by dot-notation path,
and fails if the field's value contains the alarm keyword. Mirrors the HTTP
executor's structure: times the transport call, feeds the outcome to the pure
classifier (:func:`tgmonitor.results.classify_api_content`), and converts
transport exceptions into failing Results.
"""

from __future__ import annotations

import time

import httpx

from tgmonitor.executors.base import CheckConfig, Transport
from tgmonitor.results import Result, classify_api_content


async def run_api_content_check(config: CheckConfig, transport: Transport) -> Result:
    """Run one API-content Check and classify it. Never raises.

    Requires ``config.json_field_path`` and ``config.json_keyword`` to be set;
    if either is missing, returns a failing Result (configuration error).
    """
    if not config.json_field_path or not config.json_keyword:
        return Result(
            success=False,
            reason="api_content check requires json_field_path and json_keyword",
        )

    start = time.monotonic()
    transport_error: str | None = None
    status_code = 0
    body_text = ""
    try:
        status_code, body_text = await transport.request(
            config.target, timeout_s=config.timeout_s, follow_redirects=config.follow_redirects
        )
    except httpx.TimeoutException:
        transport_error = f"timed out after {config.timeout_s}s"
    except httpx.HTTPError as exc:
        transport_error = f"connection failed: {exc.__class__.__name__}"

    latency_ms = int((time.monotonic() - start) * 1000)

    if transport_error is not None:
        return Result(success=False, reason=transport_error, latency_ms=latency_ms)

    return classify_api_content(
        body_text,
        config.json_field_path,
        config.json_keyword,
        status_code=status_code,
        latency_ms=latency_ms,
    )
