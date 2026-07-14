"""Check executors — the testable seam (Seam B in the launch spec).

Each executor is a pure-ish function ``run_check(config, transport) -> Result``.
The only seam is the ``Transport``: real runs inject an ``httpx``-backed
transport; tests inject a fake. No real network in tests (spec mandate).

The classification logic itself (:func:`tgmonitor.results.classify_http`) is a
pure function tested independently. The executor wires the transport to the
classifier and converts transport errors into failing Results with reasons.
"""

from __future__ import annotations

from .base import CheckConfig, Transport, run_check
from .http import HttpTransport, run_http_check

__all__ = [
    "CheckConfig",
    "HttpTransport",
    "Transport",
    "run_check",
    "run_http_check",
]
