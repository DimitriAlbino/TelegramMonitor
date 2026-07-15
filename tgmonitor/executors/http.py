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

    The client carries a request event hook that re-validates every request's
    destination against the SSRF blocklist (#22), including redirect targets
    when ``follow_redirects`` is in effect — so an external URL that 302s to an
    internal address is refused, regardless of the per-monitor redirect flag or
    the client-level follow default.
    """

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def request(
        self, url: str, *, timeout_s: float, follow_redirects: bool = False
    ) -> tuple[int, str]:
        # SSRF guard (#22): refuse internal targets. The request hook below
        # re-checks redirect targets; this pre-check covers the initial URL and
        # gives a clean failure for blocked literals before opening a client.
        from tgmonitor.executors.ssrf import assert_safe_destination

        assert_safe_destination(url)
        client = self._client or httpx.AsyncClient(
            timeout=timeout_s,
            follow_redirects=follow_redirects,
            event_hooks={"request": [_ssrf_request_hook]},
        )
        owned = self._client is None
        try:
            resp = await client.get(url, timeout=timeout_s, follow_redirects=follow_redirects)
            return resp.status_code, resp.text
        finally:
            if owned:
                await client.aclose()


async def _ssrf_request_hook(request: httpx.Request) -> None:
    """httpx request event hook: re-validate each destination (#22).

    Fires for the initial request and for every redirect hop when redirects are
    followed. Raises :class:`DestinationBlocked` on an internal target, which
    httpx surfaces to the caller (the executor maps it to a failing Result via
    the run_http_check/run_api_content_check exception handling).
    """
    from tgmonitor.executors.ssrf import assert_safe_destination

    assert_safe_destination(str(request.url))


async def run_http_check(config: CheckConfig, transport: Transport) -> Result:
    """Run one HTTP Check and classify it. Never raises.

    Times the transport call via ``time.monotonic``, feeds the outcome to the
    pure classifier (:func:`tgmonitor.results.classify_http`), and converts any
    transport exception into a failing Result with a human reason — the engine
    records every probe outcome, even failures.

    The target is checked against the SSRF blocklist (#22) before any request;
    redirect targets are re-validated by the transport's request hook.
    """
    from tgmonitor.executors.ssrf import DestinationBlocked, assert_safe_destination

    try:
        assert_safe_destination(config.target)
    except DestinationBlocked as exc:
        return Result(success=False, reason=f"target blocked: {exc}")

    start = time.monotonic()
    transport_error: str | None = None
    status_code = 0
    body_text = ""
    try:
        status_code, body_text = await transport.request(
            config.target, timeout_s=config.timeout_s, follow_redirects=config.follow_redirects
        )
    except DestinationBlocked as exc:
        # Raised by the SSRF hook for a redirect to an internal target (#22).
        return Result(success=False, reason=f"target blocked: {exc}", latency_ms=0)
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
