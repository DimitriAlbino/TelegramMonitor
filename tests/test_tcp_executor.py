"""Seam B tests — the TCP Check executor with a fake transport.

Mirrors the HTTP executor tests: success on connect, failure on timeout, failure
on refusal. No real network.
"""

from __future__ import annotations

from tests.conftest import FakeTransport
from tgmonitor.executors.base import CheckConfig, Transport, run_check
from tgmonitor.executors.tcp import run_tcp_check


def tcp_config(
    target: str = "example.com:443", *, monitor_id: int = 1, timeout_s: float = 5.0
) -> CheckConfig:
    return CheckConfig(
        monitor_id=monitor_id, check_kind="tcp", target=target, tcp_timeout_s=timeout_s
    )


async def test_tcp_connect_success() -> None:
    transport: Transport = FakeTransport()  # request() returns (0,""), no raise
    result = await run_tcp_check(tcp_config(), transport)
    assert result.success is True
    assert "connected" in result.reason


async def test_tcp_timeout_failure() -> None:
    transport: Transport = FakeTransport(raise_on={"example.com:443": TimeoutError()})
    result = await run_tcp_check(tcp_config(timeout_s=3.0), transport)
    assert result.success is False
    assert "timed out" in result.reason
    assert "3.0s" in result.reason


async def test_tcp_refused_failure() -> None:
    transport: Transport = FakeTransport(raise_on={"example.com:443": ConnectionRefusedError()})
    result = await run_tcp_check(tcp_config(), transport)
    assert result.success is False
    assert "failed" in result.reason


async def test_tcp_dns_failure() -> None:
    transport: Transport = FakeTransport(
        raise_on={"nonexistent.invalid:443": OSError("name resolution failed")}
    )
    result = await run_tcp_check(tcp_config("nonexistent.invalid:443"), transport)
    assert result.success is False
    assert "failed" in result.reason


async def test_dispatcher_runs_tcp_kind() -> None:
    """run_check routes to the tcp executor by check_kind."""
    transport: Transport = FakeTransport()
    result = await run_check(tcp_config(), transport)
    assert result.success is True


def test_tcp_config_default_timeout() -> None:
    cfg = CheckConfig(monitor_id=1, check_kind="tcp", target="example.com:443")
    assert cfg.tcp_timeout_s == 5.0


async def test_tcp_internal_target_refused() -> None:
    """TCP must not be usable as an internal port scanner (#22)."""
    transport: Transport = FakeTransport()
    result = await run_tcp_check(tcp_config("169.254.169.254:80"), transport)
    assert result.success is False
    assert "blocked" in result.reason


async def test_tcp_transport_closes_socket_on_success(monkeypatch) -> None:
    """The writer is closed on a successful probe — no socket/FD leak (#22).

    Bypass the SSRF guard (which would block loopback) so we can exercise the
    real open_connection + close path against a local listener, and assert the
    returned writer's transport was closed.
    """
    import asyncio

    from tgmonitor.executors import tcp as tcp_mod

    # Neutralize the SSRF guard for this test: we are connecting to our own
    # ephemeral listener to observe writer.close(), not probing an internal host.
    def _noop_assert(*a, **k):
        return None

    monkeypatch.setattr(tcp_mod, "assert_safe_destination", _noop_assert, raising=False)
    # The guard is imported by name inside TcpTransport.request; patch the
    # source module so the import resolves to a no-op.
    import tgmonitor.executors.ssrf as ssrf_mod

    monkeypatch.setattr(ssrf_mod, "assert_safe_destination", _noop_assert)

    opened_writers: list = []

    async def handler(_reader, writer):
        # Keep the server side open so we can observe the client closing its end.
        await asyncio.sleep(0.5)

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        transport = tcp_mod.TcpTransport()
        # Capture the writer the transport opens by patching open_connection.
        real_open = asyncio.open_connection

        async def capturing_open(host, port, **kw):
            r, w = await real_open(host, port, **kw)
            opened_writers.append(w)
            return r, w

        monkeypatch.setattr(tcp_mod.asyncio, "open_connection", capturing_open)
        await transport.request(f"127.0.0.1:{port}", timeout_s=2.0)
    finally:
        server.close()
        await server.wait_closed()

    assert opened_writers, "the transport should have opened a connection"
    # The writer the transport opened must have been closed (transport closed).
    assert opened_writers[0].is_closing()
