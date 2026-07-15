"""Shared pytest fixtures and fake transports.

Per the launch spec's Testing Decisions, Seam B tests inject a fake transport —
no real network in tests.
"""

from __future__ import annotations

from tgmonitor.executors.base import CheckConfig


class FakeTransport:
    """A scripted transport for executor tests.

    - ``responses`` maps url → (status, body) returned on request.
    - ``raise_on`` maps url → exception instance raised on request.
    - ``delays`` maps url → seconds to sleep (to test latency classification).

    One request per url is the common case; the same url returning repeatedly
    just looks up the same entry.
    """

    def __init__(
        self,
        responses: dict[str, tuple[int, str]] | None = None,
        raise_on: dict[str, Exception] | None = None,
        delays: dict[str, float] | None = None,
    ) -> None:
        self.responses = responses or {}
        self.raise_on = raise_on or {}
        self.delays = delays or {}
        self.requested: list[str] = []

    async def request(
        self, url: str, *, timeout_s: float, follow_redirects: bool = False
    ) -> tuple[int, str]:
        import asyncio

        self.requested.append(url)
        if url in self.delays:
            await asyncio.sleep(self.delays[url])
        if url in self.raise_on:
            raise self.raise_on[url]
        return self.responses.get(url, (200, ""))


def http_config(
    target: str = "https://example.com",
    *,
    monitor_id: int = 1,
    expected_status: int = 200,
    body_contains: str | None = None,
    max_latency_ms: int | None = None,
    timeout_s: float = 10.0,
) -> CheckConfig:
    return CheckConfig(
        monitor_id=monitor_id,
        check_kind="http",
        target=target,
        expected_status=expected_status,
        body_contains=body_contains,
        max_latency_ms=max_latency_ms,
        timeout_s=timeout_s,
    )


def api_content_config(
    target: str = "https://api.example.com/status",
    *,
    monitor_id: int = 1,
    json_field_path: str = "mode",
    json_keyword: str = "stale",
    timeout_s: float = 10.0,
) -> CheckConfig:
    return CheckConfig(
        monitor_id=monitor_id,
        check_kind="api_content",
        target=target,
        json_field_path=json_field_path,
        json_keyword=json_keyword,
        timeout_s=timeout_s,
    )
