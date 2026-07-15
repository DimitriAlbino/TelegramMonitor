"""Tests for the post-incident summary delivery (#21).

The summary must actually be sent before ``summary_sent_at`` is stamped; today
the close path stamped the flag before the sink ran and the sink gated on the
flag being null, so the send was always skipped and the flag recorded the
summary as sent with no retry. The flag was also set when the monitor was muted
or delivery was deferred.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

import tgmonitor.telegram.alerting as alerting
from tgmonitor.incidents import Action
from tgmonitor.worker.alerting import AlertIntent


class _FakeChannel:
    """Records sends; controllable success per call index."""

    def __init__(self, results: list[bool] | None = None) -> None:
        self.sent: list[str] = []
        self.results = results  # if None, always True

    async def send(self, _chat_id, text):
        self.sent.append(text)
        if self.results is None:
            return True
        idx = len(self.sent) - 1
        return self.results[idx] if idx < len(self.results) else True


class _FakeIncident:
    def __init__(self) -> None:
        self.id = 99
        self.monitor_id = 1
        self.opened_at = datetime(2026, 7, 15, 10, 0, tzinfo=UTC)
        self.closed_at = datetime(2026, 7, 15, 10, 10, tzinfo=UTC)
        self.open_reason = "HTTP 500"
        self.summary_sent_at: datetime | None = None
        self.failed_check_count = 3


class _FakeMonitor:
    def __init__(self, *, critical=False, muted=False) -> None:
        self.id = 1
        self.name = "Billing API"
        self.user_id = 1
        self.critical = critical
        self.muted = muted


class _FakeUser:
    def __init__(self, *, quiet=False, chat_id="123") -> None:
        self.id = 1
        self.telegram_chat_id = chat_id
        self.quiet_hours_start = "22:00" if quiet else None
        self.quiet_hours_end = "07:00" if quiet else None
        self.quiet_hours_tz = "UTC" if quiet else None


class _FakeResult:
    def __init__(self, scalar: int = 0) -> None:
        self._scalar = scalar

    def scalar_one(self):
        return self._scalar


class _FakeSession:
    def __init__(self, monitor, user, incident) -> None:
        self._monitor = monitor
        self._user = user
        self._incident = incident

    async def get(self, model, pk):
        name = getattr(model, "__name__", "")
        if name == "Monitor":
            return self._monitor
        if name == "User":
            return self._user
        if name == "Incident":
            return self._incident
        return None

    async def execute(self, _stmt):
        # render_post_incident_summary counts failed checks; return 0.
        return _FakeResult(scalar=0)

    async def commit(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _patch_db(monkeypatch, monitor, user, incident):
    session = _FakeSession(monitor, user, incident)

    class _Maker:
        def __call__(self):
            return session

    def fake_factory():
        return _Maker()

    import tgmonitor.db as db_mod

    monkeypatch.setattr(db_mod, "session_factory", fake_factory)


@pytest.fixture(autouse=True)
def _reset_queue():
    alerting._digests.clear()
    yield
    alerting._digests.clear()


async def test_close_delivers_summary_and_stamps_on_success(monkeypatch) -> None:
    """Closing an Incident delivers the summary and stamps summary_sent_at (#21)."""
    inc = _FakeIncident()
    _patch_db(monkeypatch, _FakeMonitor(), _FakeUser(), inc)
    ch = _FakeChannel()
    sink = alerting.make_alert_sink(channel=ch)

    await sink(AlertIntent(1, Action.CLOSE_INCIDENT, inc.id, "recovered"))

    # Two sends: the recovery one-liner, then the summary.
    assert len(ch.sent) == 2
    assert "Post-incident summary" in ch.sent[1]
    assert inc.summary_sent_at is not None  # stamped only after success


async def test_summary_not_stamped_when_send_fails(monkeypatch) -> None:
    """If delivery fails, summary_sent_at stays null so a retry can happen (#21)."""
    inc = _FakeIncident()
    _patch_db(monkeypatch, _FakeMonitor(), _FakeUser(), inc)
    # recovery send ok, summary send fails.
    ch = _FakeChannel(results=[True, False])
    sink = alerting.make_alert_sink(channel=ch)

    await sink(AlertIntent(1, Action.CLOSE_INCIDENT, inc.id, "recovered"))

    assert inc.summary_sent_at is None


async def test_summary_not_sent_when_deferred(monkeypatch) -> None:
    """Quiet-hours-deferred delivery must not send the summary or stamp (#21)."""
    inc = _FakeIncident()
    _patch_db(monkeypatch, _FakeMonitor(), _FakeUser(quiet=True), inc)
    ch = _FakeChannel()
    # Inject a clock inside the 22:00-07:00 window so delivery is deferred.
    sink = alerting.make_alert_sink(
        channel=ch, now=lambda: datetime(2026, 7, 15, 3, 0, tzinfo=UTC)
    )

    await sink(AlertIntent(1, Action.CLOSE_INCIDENT, inc.id, "recovered"))

    assert ch.sent == []  # nothing sent immediately
    assert inc.summary_sent_at is None  # not stamped
    # The recovery one-liner was queued for the digest instead.
    assert alerting._digests.get(1) is not None


async def test_summary_not_sent_again_if_already_stamped(monkeypatch) -> None:
    """A second close intent for an already-summarized incident does not resend."""
    inc = _FakeIncident()
    inc.summary_sent_at = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)
    _patch_db(monkeypatch, _FakeMonitor(), _FakeUser(), inc)
    ch = _FakeChannel()
    sink = alerting.make_alert_sink(channel=ch)

    await sink(AlertIntent(1, Action.CLOSE_INCIDENT, inc.id, "recovered"))

    # Only the recovery one-liner; the summary guard holds.
    assert len(ch.sent) == 1
