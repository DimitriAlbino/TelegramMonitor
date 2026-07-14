"""Executor seam: the protocol and dispatcher.

The design splits cleanly along two lines so the test surface is small:

1. **Classification** (pure): :func:`tgmonitor.results.classify_http` turns a
   (status, body, latency) triple into a Result. No I/O. Heavily unit-tested.

2. **Execution** (impure, thin): the executor fetches bytes via a ``Transport``
   and feeds them to the classifier, translating any transport exception into a
   failing Result with a human reason.

``run_check`` is the dispatcher the worker calls; it reads ``check_kind`` off
the :class:`CheckConfig` and routes to the right executor. Today only ``http``
is implemented (T1 scope); ``tcp`` arrives in T3.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from tgmonitor.results import Result


@dataclass(frozen=True, slots=True)
class CheckConfig:
    """A Monitor's kind + target + kind-specific knobs, unpacked for the executor.

    Built from a Monitor row by the worker; the executor never touches the ORM.
    """

    monitor_id: int
    check_kind: str  # "http" | "tcp"
    target: str
    # HTTP knobs (ignored for tcp).
    expected_status: int = 200
    body_contains: str | None = None
    max_latency_ms: int | None = None
    timeout_s: float = 10.0
    # TCP knobs (ignored for http).
    tcp_timeout_s: float = 5.0


@runtime_checkable
class Transport(Protocol):
    """The only I/O seam.

    A transport performs one request and returns a response. Real
    implementation: :class:`HttpTransport` (httpx). Tests inject a fake.
    """

    async def request(self, url: str, *, timeout_s: float) -> tuple[int, str]:
        """GET ``url``, returning ``(status_code, body_text)``.

        Implementations raise on transport-level failure (connection refused,
        timeout, DNS). The executor catches and converts to a failing Result.
        """
        ...


async def run_check(config: CheckConfig, transport: Transport) -> Result:
    """Dispatch a Check to the right executor by ``check_kind``.

    Kept import-light: the http executor is imported lazily so the module graph
    stays clean and tcp (T3) slots in here without touching the http path.
    """
    from tgmonitor.executors.http import run_http_check

    if config.check_kind == "http":
        return await run_http_check(config, transport)
    # tcp lands in T3. Until then, an unknown kind is a configuration error,
    # surfaced as a failing Result rather than a crash — the engine must never
    # die on one bad Monitor.
    return Result(
        success=False,
        reason=f"unsupported check_kind {config.check_kind!r}",
    )
