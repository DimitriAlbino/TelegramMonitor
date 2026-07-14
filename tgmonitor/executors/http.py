"""HTTP Check executor.

:func:`run_http_check` is the impure-but-thin executor: it times the transport
call, feeds the outcome to the pure classifier
(:func:`tgmonitor.results.classify_http`), and converts any transport exception
into a failing Result with a reason. The classification rules themselves live in
the pure module and are tested there.

Error → Result mapping (so the engine keeps a record of why a probe failed):
- ``httpx.ConnectError`` / connection refused  → "connection failed: …"
- ``httpx.TimeoutException``                    → "timed out after {timeout}s"
- any other exception                           → "{type}: {msg}"
"""

from __future__ import annotations

import time

import httpx

from tgmonitor.executors.base import CheckConfig, Transport
from tgmonitor.results import Result, classify_http


class HttpTransport:
    """The real transport: an ``httpx.AsyncClient`` GET.

    A thin adapter so :class:`CheckConfig`/``Transport`` stay backend-agnostic.
    Reuses a long-lived client for connection pooling; tests pass a fake.
    """

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def request(self, url: str, *, timeout_s: float) -> tuple[int, str]:
        client = self._client or httpx.AsyncClient(timeout=timeout_s)
        owned = self._client is None
        try:
            resp = await client.get(url, timeout=timeout_s)
            return resp.status_code, resp.text
        finally:
            if owned:
                await client.aclose()


async def run_http_check(config: CheckConfig, transport: Transport) -> Result:
    """Run one HTTP Check and classify it. Never raises.

    Times the transport call via ``time.monotonic``, feeds the outcome to the
    pure classifier (:func:`tgmonitor.results.classify_http`), and converts any
    transport exception into a failing Result with a human reason — the engine
    records every probe outcome, even failures.
    """
    start = time.monotonic()
    transport_error: str | None = None
    status_code = 0
    body_text = ""
    try:
        status_code, body_text = await transport.request(config.target, timeout_s=config.timeout_s)
    except httpx.TimeoutException:
        transport_error = f"timed out after {config.timeout_s}s"
    except httpx.HTTPError as exc:
        transport_error = f"connection failed: {exc.__class__.__name__}"

    latency_ms = int((time.monotonic() - start) * 1000)

    if transport_error is not None:
        return Result(success=False, reason=transport_error, latency_ms=latency_ms)

    return classify_http(
        status_code,
        expected_status=config.expected_status,
        body_contains=config.body_contains,
        body_text=body_text,
        latency_ms=latency_ms,
        max_latency_ms=config.max_latency_ms,
    )
