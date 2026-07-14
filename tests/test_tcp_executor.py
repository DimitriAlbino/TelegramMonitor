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
