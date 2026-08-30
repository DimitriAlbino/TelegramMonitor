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

import ipaddress
import time
from urllib.parse import urlparse

import httpx

from tgmonitor.executors.base import CheckConfig, Transport
from tgmonitor.results import Result, classify_http

# Cap on redirect hops we follow manually (each hop is independently re-pinned).
MAX_REDIRECTS = 5


class HttpTransport:
    """The real transport: an ``httpx.AsyncClient`` GET with SSRF-safe pinning.

    A thin adapter so :class:`CheckConfig`/``Transport`` stay backend-agnostic.
    Reuses a long-lived client for connection pooling; tests pass a fake.

    Redirects are followed manually so every hop is resolved, validated, and
    **pinned to vetted IPs** (#39): we connect to the exact addresses we checked
    (Host header + TLS SNI preserved for the original hostname), which closes
    the DNS-rebinding TOCTOU that a check-by-name guard leaves open — a name can
    no longer answer public to the guard and internal to the client.

    Each hop's candidates (IPv4 first, then IPv6 — see ``pick_safe_ips``) are
    tried in order, falling back to the next candidate only on
    :class:`httpx.ConnectError` (instantly refused / unreachable), so a
    dual-stack target stays monitorable from a single-stack network. Timeouts
    are deliberately *not* retried per candidate — a blackholed address would
    multiply the probe's wall clock — and surface immediately. An internal
    target at any hop raises DestinationBlocked.
    """

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def request(
        self, url: str, *, timeout_s: float, follow_redirects: bool = False
    ) -> tuple[int, str]:
        client = self._client or httpx.AsyncClient(timeout=timeout_s)
        owned = self._client is None
        try:
            logical = url  # hostname-based URL; redirects resolve against this
            resp: httpx.Response | None = None
            for _hop in range(MAX_REDIRECTS + 1):
                resp = await self._get_pinned(client, logical, timeout_s)
                location = resp.headers.get("location")
                if not (follow_redirects and resp.is_redirect and location):
                    break
                logical = str(httpx.URL(logical).join(location))
            assert resp is not None
            return resp.status_code, resp.text
        finally:
            if owned:
                await client.aclose()

    async def _get_pinned(
        self, client: httpx.AsyncClient, logical: str, timeout_s: float
    ) -> httpx.Response:
        """GET one hop, attempting each pinned candidate in order.

        Falls back to the next vetted address on ConnectError only; if every
        candidate fails, the last connect error propagates.
        """
        last_error: httpx.ConnectError | None = None
        for pinned_url, headers, sni in _pin_candidates(logical):
            extensions = {"sni_hostname": sni} if sni else None
            try:
                return await client.get(
                    pinned_url,
                    timeout=timeout_s,
                    follow_redirects=False,
                    headers=headers,
                    extensions=extensions,
                )
            except httpx.ConnectError as exc:
                last_error = exc
        assert last_error is not None  # _pin_candidates never returns an empty list
        raise last_error


def _pin_candidates(url: str) -> list[tuple[str, dict[str, str] | None, str | None]]:
    """Return the ``(request_url, headers, sni_hostname)`` candidates for a hop.

    Validates the destination and, for a hostname, rewrites the URL to each
    vetted IP (ordered IPv4-first by :func:`pick_safe_ips`) while returning the
    headers (``Host``, plus a reconstructed ``Authorization`` if the URL carried
    basic-auth userinfo — which the rewrite strips) and SNI hostname to preserve
    routing, credentials, and TLS. Raises :class:`DestinationBlocked` for an
    internal destination. An IP literal is validated and returned as the single
    candidate; an unresolvable hostname yields a single by-name candidate (it
    will fail at connect). For the pass-through cases httpx handles any URL
    userinfo.
    """
    from tgmonitor.executors.ssrf import DestinationBlocked, is_blocked_ip, pick_safe_ips

    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        raise DestinationBlocked(f"could not parse host from target {url!r}")

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if is_blocked_ip(literal):
            raise DestinationBlocked(f"target IP {literal} is a blocked internal address")
        return [(url, None, None)]

    safe_ips = pick_safe_ips(host)  # raises if internal; [] if unresolvable
    if not safe_ips:
        return [(url, None, None)]

    candidates: list[tuple[str, dict[str, str] | None, str | None]] = []
    for safe_ip in safe_ips:
        ip_netloc = f"[{safe_ip}]" if ":" in safe_ip else safe_ip
        if parsed.port:
            ip_netloc = f"{ip_netloc}:{parsed.port}"
        pinned_url = parsed._replace(netloc=ip_netloc).geturl()
        headers = {"Host": f"{host}:{parsed.port}" if parsed.port else host}
        # Rewriting netloc to the IP drops any user:pass@ userinfo; reconstruct the
        # basic-auth header so credential-bearing targets keep working.
        if parsed.username is not None:
            import base64

            creds = f"{parsed.username}:{parsed.password or ''}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(creds).decode()
        candidates.append((pinned_url, headers, host))
    return candidates


async def run_http_check(config: CheckConfig, transport: Transport) -> Result:
    """Run one HTTP Check and classify it. Never raises.

    Times the transport call via ``time.monotonic``, feeds the outcome to the
    pure classifier (:func:`tgmonitor.results.classify_http`), and converts any
    transport exception into a failing Result with a human reason — the engine
    records every probe outcome, even failures.

    The target is checked against the SSRF blocklist (#22) before any request;
    the transport additionally pins each hop to a vetted IP (#39), so an internal
    target — direct or via redirect — surfaces as a DestinationBlocked failure.
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
        # Keep the message, not just the class: "All connection attempts failed"
        # vs "refused" distinguishes an unreachable network from a closed port.
        transport_error = f"connection failed: {exc.__class__.__name__}: {exc}"

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
