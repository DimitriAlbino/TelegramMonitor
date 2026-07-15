"""TCP Check executor.

Opens a socket to host:port, succeeds on connect within the timeout, and fails
on timeout or refusal. Like the HTTP executor, the only I/O seam is the
transport; the real one uses ``asyncio.open_connection``, tests inject a fake.

CONTEXT.md Check Kind: "TCP — open a socket to host:port."
"""

from __future__ import annotations

import asyncio
import contextlib

from tgmonitor.executors.base import CheckConfig, Transport
from tgmonitor.results import Result


class TcpTransport:
    """The real TCP transport: ``asyncio.open_connection`` to host:port.

    The target string is ``host:port``. A thin adapter so the executor stays
    backend-agnostic; tests pass a fake.

    The destination is checked against the SSRF blocklist (#22) before
    connecting, and the writer is closed on every path (success and failure) so
    no socket/FD is leaked.
    """

    async def request(
        self, url: str, *, timeout_s: float, follow_redirects: bool = False
    ) -> tuple[int, str]:
        # For TCP, ``url`` is actually ``host:port``. Resolve and connect; raise
        # on failure (the executor maps exceptions to a failing Result).
        host, _, port_str = url.rpartition(":")
        if not host or not port_str:
            raise ValueError(f"invalid host:port target: {url!r}")
        # SSRF guard (#22): refuse internal/link-loopback targets. This also
        # stops TCP being used as an internal port scanner.
        from tgmonitor.executors.ssrf import assert_safe_destination

        assert_safe_destination(url, is_host_port=True)
        port = int(port_str)
        # Always close the writer so the socket is not leaked (#22). The reader
        # is closed implicitly when the writer closes the transport.
        writer = None
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=timeout_s
            )
        finally:
            if writer is not None:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
        # No HTTP status code for a TCP probe; report a sentinel 0 on success.
        return 0, ""


async def run_tcp_check(config: CheckConfig, transport: Transport) -> Result:
    """Run one TCP connect Check and classify it. Never raises.

    Uses the transport's timeout as the TCP timeout. A successful connect is a
    success; a timeout or refusal is a failure with a human reason. An SSRF-
    blocked destination (#22) is a configuration failure.
    """
    from tgmonitor.executors.ssrf import DestinationBlocked, assert_safe_destination

    # SSRF guard (#22) at the executor level — checked before any connect so it
    # applies regardless of transport (real or fake), matching the HTTP and
    # api_content executors. Stops TCP being used as an internal port scanner.
    try:
        assert_safe_destination(config.target, is_host_port=True)
    except DestinationBlocked as exc:
        return Result(success=False, reason=f"target blocked: {exc}")

    timeout = config.tcp_timeout_s
    try:
        await transport.request(config.target, timeout_s=timeout)
    except DestinationBlocked as exc:
        return Result(success=False, reason=f"target blocked: {exc}")
    except TimeoutError:
        return Result(
            success=False,
            reason=f"tcp connect timed out after {timeout}s",
        )
    except OSError as exc:
        # Connection refused, DNS failure, etc. — all subclass OSError.
        return Result(
            success=False,
            reason=f"tcp connect failed: {exc.__class__.__name__}".lower(),
        )
    except ValueError as exc:
        return Result(success=False, reason=str(exc))

    return Result(success=True, reason="connected")
