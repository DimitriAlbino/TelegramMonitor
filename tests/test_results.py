"""Pure classification tests — :func:`tgmonitor.results.classify_http`.

No I/O, no transport. These pin the classification rules independently of the
executor, per the launch spec's Seam B note.
"""

from __future__ import annotations

import dataclasses

import pytest

from tgmonitor.results import Result, classify_http


def test_status_match_success() -> None:
    r = classify_http(200, body_text="anything")
    assert r == Result(success=True, reason="ok", status_code=200, latency_ms=None)


def test_status_mismatch_failure() -> None:
    r = classify_http(503, body_text="", expected_status=200)
    assert r.success is False
    assert "expected 200" in r.reason
    assert r.status_code == 503


def test_body_keyword_missing_failure() -> None:
    r = classify_http(200, body_contains="ok", body_text="error")
    assert r.success is False
    assert "ok" in r.reason


def test_body_keyword_present_success() -> None:
    r = classify_http(200, body_contains="ok", body_text="all ok here")
    assert r.success is True


def test_max_latency_exceeded_failure() -> None:
    r = classify_http(200, body_text="", latency_ms=750, max_latency_ms=500)
    assert r.success is False
    assert "750" in r.reason and "500" in r.reason


def test_max_latency_within_success() -> None:
    r = classify_http(200, body_text="", latency_ms=100, max_latency_ms=500)
    assert r.success is True


def test_no_max_latency_means_unbounded() -> None:
    r = classify_http(200, body_text="", latency_ms=999_999, max_latency_ms=None)
    assert r.success is True


def test_result_is_frozen() -> None:
    r = classify_http(200, body_text="")
    # FrozenInstanceError is raised on mutation of a frozen dataclass.
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.success = False  # type: ignore[misc]
