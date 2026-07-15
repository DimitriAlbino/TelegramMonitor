"""Tests for the quiet-hours digest flusher (#20).

``flush_due_digests`` is called by the worker tick loop. It must deliver
non-critical alerts deferred during a quiet window once the window ends, leave
messages queued (not dropped) while still inside the window, and never grow the
queue unbounded across a flush.

The clock and user-lookup seams are injected so no real Postgres or wall-clock
dependency runs here.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

import tgmonitor.telegram.alerting as alerting
from tgmonitor.telegram.alerting import UserLookup


class _FakeChannel:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, chat_id, text):
        self.sent.append((chat_id, text))
        return True


class _FakeUser:
    def __init__(self, chat_id="123", start="22:00", end="07:00", tz="UTC") -> None:
        self.telegram_chat_id = chat_id
        self.quiet_hours_start = start
        self.quiet_hours_end = end
        self.quiet_hours_tz = tz


class _FakeLookup(UserLookup):
    def __init__(self, user: _FakeUser | None) -> None:
        self._user = user

    async def get(self, _user_id):
        return self._user


@pytest.fixture(autouse=True)
def _reset_queue():
    alerting._digests.clear()
    yield
    alerting._digests.clear()


async def test_deferred_digest_delivered_when_window_ended() -> None:
    """An alert deferred during the window is delivered once it ends (#20)."""
    alerting._digests[1] = ["🔴 A down", "🔴 B down"]
    # User window 22:00-07:00 UTC; now is 12:00 → outside.
    lookup = _FakeLookup(_FakeUser())
    ch = _FakeChannel()

    n = await alerting.flush_due_digests(
        channel=ch,
        now_utc=datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
        user_lookup=lookup,
    )

    assert n == 1
    assert len(ch.sent) == 1
    body = ch.sent[0][1]
    assert "A down" in body and "B down" in body
    assert alerting._digests.get(1) is None  # queue drained


async def test_still_in_window_requeues_and_does_not_send() -> None:
    """If still inside the window at flush time, messages are re-queued (#20)."""
    alerting._digests[1] = ["🔴 A down"]
    lookup = _FakeLookup(_FakeUser())
    ch = _FakeChannel()
    # 03:00 UTC is inside 22:00-07:00.
    n = await alerting.flush_due_digests(
        channel=ch,
        now_utc=datetime(2026, 7, 15, 3, 0, tzinfo=UTC),
        user_lookup=lookup,
    )

    assert n == 0
    assert ch.sent == []
    assert alerting._digests.get(1) == ["🔴 A down"]  # preserved, not dropped


async def test_boundary_now_equals_end_delivers() -> None:
    """At exactly the end time the window is exclusive → deliver (#20)."""
    alerting._digests[1] = ["🔴 A down"]
    lookup = _FakeLookup(_FakeUser(start="22:00", end="07:00"))
    ch = _FakeChannel()
    n = await alerting.flush_due_digests(
        channel=ch,
        now_utc=datetime(2026, 7, 15, 7, 0, tzinfo=UTC),  # == end → outside
        user_lookup=lookup,
    )
    assert n == 1


async def test_boundary_now_equals_start_defers() -> None:
    """At exactly the start time the window is inclusive → still deferred (#20)."""
    alerting._digests[1] = ["🔴 A down"]
    lookup = _FakeLookup(_FakeUser(start="22:00", end="07:00"))
    ch = _FakeChannel()
    n = await alerting.flush_due_digests(
        channel=ch,
        now_utc=datetime(2026, 7, 15, 22, 0, tzinfo=UTC),  # == start → inside
        user_lookup=lookup,
    )
    assert n == 0
    assert ch.sent == []
    assert alerting._digests.get(1) == ["🔴 A down"]


async def test_no_chat_id_drops_silently() -> None:
    """A deferred digest for a user with no linked chat is dropped (non-critical)."""
    alerting._digests[1] = ["🔴 A down"]
    lookup = _FakeLookup(_FakeUser(chat_id=None))
    ch = _FakeChannel()

    n = await alerting.flush_due_digests(
        channel=ch,
        now_utc=datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
        user_lookup=lookup,
    )

    assert n == 0
    assert alerting._digests.get(1) is None  # drained, not re-queued forever


async def test_empty_queue_is_noop() -> None:
    ch = _FakeChannel()
    n = await alerting.flush_due_digests(channel=ch)
    assert n == 0
    assert ch.sent == []
