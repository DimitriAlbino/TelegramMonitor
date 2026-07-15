"""TCP Check executor.

Opens a socket to host:port, succeeds on connect within the timeout, and fails
on timeout or refusal. Like the HTTP executor, the only I/O seam is the
transport; the real one uses ``asyncio.open_connection``, tests inject a fake.

CONTEXT.md Check Kind: "TCP — open a socket to host:port."
"""

from __future__ import annotations

import asyncio

from tgmonitor.executors.base import CheckConfig, Transport
from tgmonitor.results import Result


class TcpTransport:
    """The real TCP transport: ``asyncio.open_connection`` to host:port.

    The target string is ``host:port``. A thin adapter so the executor stays
    backend-agnostic; tests pass a fake.
    """

    async def request(
        self, url: str, *, timeout_s: float, follow_redirects: bool = False
    ) -> tuple[int, str]:
        # For TCP, ``url`` is actually ``host:port``. Resolve and connect; raise
        # on failure (the executor maps exceptions to a failing Result).
        host, _, port_str = url.rpartition(":")
        if not host or not port_str:
            raise ValueError(f"invalid host:port target: {url!r}")
        port = int(port_str)
        await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout_s)
        # No HTTP status code for a TCP probe; report a sentinel 0 on success.
        return 0, ""


async def run_tcp_check(config: CheckConfig, transport: Transport) -> Result:
    """Run one TCP connect Check and classify it. Never raises.

    Uses the transport's timeout as the TCP timeout. A successful connect is a
    success; a timeout or refusal is a failure with a human reason.
    """
    timeout = config.tcp_timeout_s
    try:
        await transport.request(config.target, timeout_s=timeout)
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
