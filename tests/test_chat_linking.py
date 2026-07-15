"""Tests for the verified Telegram chat-linking flow (#26).

Linking must prove the caller controls the chat (challenge code sent from the
target chat), must be reversible (unlink / reclaim), and must accept negative
group/supergroup chat ids. These exercise the webhook _handle_link decision
with a fake session; no real Telegram.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tgmonitor.telegram.webhook import _handle_link


class _Result:
    def __init__(self, scalar=None) -> None:
        self._scalar = scalar

    def scalar(self):
        return self._scalar


class _FakeSession:
    def __init__(self, user_by_code=None) -> None:
        self._by_code = user_by_code or {}
        self.committed = False

    async def scalar(self, stmt):
        # _handle_link issues two selects: by telegram_link_code, then by
        # telegram_chat_id. Distinguish by the WHERE clause, not the selected
        # columns (both selects list every User column).
        sql = str(stmt)
        if "telegram_link_code =" in sql:
            return self._by_code.get("linker")
        if "telegram_chat_id =" in sql:
            return self._by_code.get("existing")
        return None

    async def commit(self):
        self.committed = True


class _FakeUser:
    def __init__(
        self,
        uid=1,
        code="ABCD1234",
        expires_in_min=10,
        chat_id=None,
    ) -> None:
        self.id = uid
        self.telegram_link_code = code
        self.telegram_link_expires_at = datetime.now(UTC) + timedelta(minutes=expires_in_min)
        self.telegram_chat_id = chat_id


@pytest.fixture(autouse=True)
def _reset_link_limiter():
    """Reset the per-process link-attempt limiter so it can't leak across tests (#43)."""
    import tgmonitor.auth.ratelimit as rl

    rl._link_limiter = None
    yield
    rl._link_limiter = None


@pytest.fixture(autouse=True)
def _fake_reply(monkeypatch):
    sent: list[tuple[str, str]] = []

    async def fake_reply(chat_id, text):
        sent.append((chat_id, text))

    monkeypatch.setattr("tgmonitor.telegram.webhook._reply", fake_reply)
    return sent


async def test_link_with_valid_code_binds_chat(_fake_reply) -> None:
    linker = _FakeUser(code="ABCD1234")
    session = _FakeSession({"linker": linker})
    await _handle_link(session, "999", "abcd1234")
    assert linker.telegram_chat_id == "999"
    assert linker.telegram_link_code is None  # single-use
    assert session.committed
    assert _fake_reply  # a reply was sent
    assert "linked" in _fake_reply[0][1].lower()


async def test_link_without_code_rejected(_fake_reply) -> None:
    session = _FakeSession({"linker": _FakeUser()})
    await _handle_link(session, "999", "")
    assert _fake_reply
    assert (
        "link <code>" in _fake_reply[0][1].lower() or "request a code" in _fake_reply[0][1].lower()
    )


async def test_link_with_expired_code_rejected(_fake_reply) -> None:
    linker = _FakeUser(code="ABCD1234", expires_in_min=-5)  # expired
    session = _FakeSession({"linker": linker})
    await _handle_link(session, "999", "abcd1234")
    assert linker.telegram_chat_id is None
    assert "invalid or expired" in _fake_reply[0][1].lower()


async def test_link_with_wrong_code_rejected(_fake_reply) -> None:
    # No user has code WRONGCODE0.
    session = _FakeSession({"linker": None})
    await _handle_link(session, "999", "wrongcode0")
    assert "invalid or expired" in _fake_reply[0][1].lower()


async def test_link_accepts_negative_group_chat_id(_fake_reply) -> None:
    """Negative (group/supergroup) chat ids must be accepted (#26)."""
    linker = _FakeUser(code="ABCD1234")
    session = _FakeSession({"linker": linker})
    await _handle_link(session, "-1001234567890", "abcd1234")
    assert linker.telegram_chat_id == "-1001234567890"


async def test_link_reclaims_chat_from_another_account(_fake_reply) -> None:
    """A chat already linked elsewhere is reclaimed (previous owner unlinked)."""
    linker = _FakeUser(uid=2, code="ABCD1234")
    existing = _FakeUser(uid=1, chat_id="999")  # owns the chat
    session = _FakeSession({"linker": linker, "existing": existing})
    await _handle_link(session, "999", "abcd1234")
    assert linker.telegram_chat_id == "999"
    assert existing.telegram_chat_id is None  # reclaimed


async def test_link_attempts_are_throttled(_fake_reply) -> None:
    """After the per-chat cap, further /link attempts are refused (#43)."""
    # Wrong code each time so nothing binds; the throttle sits before the lookup.
    session = _FakeSession({"linker": _FakeUser(code="ZZZZZZZZ")})
    for _ in range(5):
        await _handle_link(session, "555", "deadbeef")
    # First 5 are processed (invalid-code reply); the 6th is throttled.
    await _handle_link(session, "555", "deadbeef")
    assert "too many" in _fake_reply[-1][1].lower()


async def test_throttle_is_per_chat(_fake_reply) -> None:
    """One chat hitting the cap does not throttle a different chat (#43)."""
    session = _FakeSession({"linker": _FakeUser(code="ZZZZZZZZ")})
    for _ in range(6):
        await _handle_link(session, "555", "deadbeef")
    # A different chat still gets a normal (invalid-code) response.
    _fake_reply.clear()
    await _handle_link(session, "777", "deadbeef")
    assert "too many" not in _fake_reply[-1][1].lower()
