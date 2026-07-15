"""Seam A tests — the pure Incident state machine.

These are the highest-value tests in the system. They pin the debounce, recovery,
flapping, and clock-injection behaviour without touching the DB, network, or a
wall clock. Cases mirror the launch spec's Seam A list and ADR-0005.
"""

from __future__ import annotations

from tgmonitor.incidents import Action, CheckOutcome, MonitorState, Thresholds, step

FAIL = CheckOutcome(success=False, reason="conn refused")
OK = CheckOutcome(success=True, reason="ok")
T0 = 1_700_000_000.0  # a fixed epoch baseline; never the wall clock


def thresholds(**kw: float | int) -> Thresholds:
    base = dict(failure_threshold=3, recovery_threshold=2, flap_opens=3, flap_window_s=600.0)
    base.update(kw)
    return Thresholds(**base)  # type: ignore[arg-type]


# --- Debounce: N failures open an Incident ---


def test_below_failure_threshold_does_not_open() -> None:
    s = MonitorState()
    th = thresholds(failure_threshold=3)
    for _ in range(2):
        t = step(s, FAIL, th, now=T0)
        s = t.state
        assert t.action is Action.NONE
        assert s.status == "ok"


def test_at_failure_threshold_opens_incident() -> None:
    s = MonitorState()
    th = thresholds(failure_threshold=3)
    action = Action.NONE
    for _ in range(3):
        t = step(s, FAIL, th, now=T0)
        s, action = t.state, t.action
    assert action is Action.OPEN_INCIDENT
    assert s.status == "down"


# --- Recovery: M successes close an Incident ---


def test_below_recovery_threshold_does_not_close() -> None:
    s = MonitorState(status="down")
    th = thresholds(recovery_threshold=2)
    t = step(s, OK, th, now=T0)
    assert t.action is Action.NONE
    assert t.state.status == "down"


def test_at_recovery_threshold_closes_incident() -> None:
    s = MonitorState(status="down")
    th = thresholds(recovery_threshold=2)
    t1 = step(s, OK, th, now=T0)
    t2 = step(t1.state, OK, th, now=T0)
    assert t2.action is Action.CLOSE_INCIDENT
    assert t2.state.status == "ok"


# --- A success mid-failure-streak resets the failure counter ---


def test_success_resets_failure_counter() -> None:
    s = MonitorState()
    th = thresholds(failure_threshold=3)
    s = step(s, FAIL, th, now=T0).state  # failures=1
    s = step(s, FAIL, th, now=T0).state  # failures=2
    s = step(s, OK, th, now=T0).state  # success → reset
    assert s.consecutive_failures == 0
    # Two more failures should NOT open (need 3).
    s = step(s, FAIL, th, now=T0).state
    t = step(s, FAIL, th, now=T0)
    assert t.action is Action.NONE
    assert t.state.status == "ok"


def test_failure_resets_success_counter() -> None:
    s = MonitorState(status="down")
    th = thresholds(recovery_threshold=2)
    s = step(s, OK, th, now=T0).state  # successes=1
    s = step(s, FAIL, th, now=T0).state  # failure → reset
    assert s.consecutive_successes == 0
    assert s.status == "down"  # still down


# --- No alert on every Check — only on transitions ---


def test_only_one_open_per_incident() -> None:
    """Once down, further failures do not re-open."""
    s = MonitorState()
    th = thresholds(failure_threshold=3)
    s = step(s, FAIL, th, now=T0).state
    s = step(s, FAIL, th, now=T0).state
    s = step(s, FAIL, th, now=T0).state  # opens
    assert s.status == "down"
    # More failures while down → no new action.
    for _ in range(5):
        t = step(s, FAIL, th, now=T0)
        s = t.state
        assert t.action is Action.NONE
        assert s.status == "down"


def test_no_action_when_already_ok_and_success() -> None:
    s = MonitorState(status="ok")
    t = step(s, OK, thresholds(), now=T0)
    assert t.action is Action.NONE


# --- Flapping ---


def test_flapping_entered_after_opens_in_window() -> None:
    """3 Incident opens within 10 min → flapping, single FLAP_START."""
    th = thresholds(failure_threshold=1, flap_opens=3, flap_window_s=600.0, recovery_threshold=1)
    s = MonitorState()
    actions: list[Action] = []
    # Simulate open→close→open→close→open sequence quickly (within window).
    # open 1
    t = step(s, FAIL, th, now=T0)
    s, actions = t.state, [*actions, t.action]
    # close 1
    t = step(s, OK, th, now=T0 + 1)
    s, actions = t.state, [*actions, t.action]
    # open 2
    t = step(s, FAIL, th, now=T0 + 2)
    s, actions = t.state, [*actions, t.action]
    # close 2
    t = step(s, OK, th, now=T0 + 3)
    s, actions = t.state, [*actions, t.action]
    # open 3 → should trigger flapping instead of a normal open
    t = step(s, FAIL, th, now=T0 + 4)
    s, actions = t.state, [*actions, t.action]

    assert actions == [
        Action.OPEN_INCIDENT,
        Action.CLOSE_INCIDENT,
        Action.OPEN_INCIDENT,
        Action.CLOSE_INCIDENT,
        Action.FLAP_START,
    ]
    assert s.status == "flapping"


def test_flapping_suppresses_new_opens() -> None:
    """While flapping, failures do not open Incidents."""
    s = MonitorState(status="flapping", recent_opens=[T0, T0, T0])
    th = thresholds()
    for _ in range(5):
        t = step(s, FAIL, th, now=T0 + 100)
        s = t.state
        assert t.action is Action.NONE
        assert s.status == "flapping"


def test_flapping_ends_after_stabilization() -> None:
    """After N consecutive successes while flapping, a single FLAP_END."""
    th = thresholds(flap_stabilization=3)
    s = MonitorState(status="flapping")
    t = step(s, OK, th, now=T0)
    s = t.state
    assert t.action is Action.NONE
    t = step(s, OK, th, now=T0)
    s = t.state
    assert t.action is Action.NONE
    t = step(s, OK, th, now=T0)
    assert t.action is Action.FLAP_END
    assert t.state.status == "ok"
    assert t.state.recent_opens == []


def test_flapping_failure_resets_stabilization() -> None:
    th = thresholds(flap_stabilization=3)
    s = MonitorState(status="flapping")
    s = step(s, OK, th, now=T0).state  # 1 success
    s = step(s, OK, th, now=T0).state  # 2 successes
    t = step(s, FAIL, th, now=T0)  # failure resets
    assert t.state.consecutive_successes == 0
    assert t.state.status == "flapping"
    assert t.action is Action.NONE


# --- Flapping is not a permanent trap (#27) ---


def test_flapping_then_hard_down_opens_incident() -> None:
    """A flapping monitor that then fails persistently must open an Incident.

    While flapping, ``flap_hard_down`` consecutive failures transition out of
    flapping into a real down state and page (#27) — flapping must not be a
    permanent silence trap.
    """
    th = thresholds(failure_threshold=3, recovery_threshold=2, flap_hard_down=5)
    s = MonitorState(status="flapping")
    action = Action.NONE
    for _ in range(th.flap_hard_down):
        t = step(s, FAIL, th, now=T0)
        s, action = t.state, t.action
    assert action is Action.OPEN_INCIDENT
    assert s.status == "down"


def test_flapping_failure_below_hard_down_stays_flapping() -> None:
    """Failures during flapping below the hard-down threshold stay silent."""
    th = thresholds(flap_hard_down=5)
    s = MonitorState(status="flapping")
    for _ in range(th.flap_hard_down - 1):
        t = step(s, FAIL, th, now=T0)
        s = t.state
        assert t.action is Action.NONE
        assert s.status == "flapping"


def test_flapping_recovery_still_ends_flapping() -> None:
    """The normal stabilization exit still works alongside the hard-down exit."""
    th = thresholds(flap_stabilization=3, flap_hard_down=5)
    s = MonitorState(status="flapping")
    s = step(s, OK, th, now=T0).state
    s = step(s, OK, th, now=T0).state
    t = step(s, OK, th, now=T0)
    assert t.action is Action.FLAP_END
    assert t.state.status == "ok"


def test_opens_outside_window_do_not_count_toward_flapping() -> None:
    """An open older than flap_window_s is pruned and doesn't contribute."""
    th = thresholds(failure_threshold=1, recovery_threshold=1, flap_opens=3, flap_window_s=600.0)
    s = MonitorState()
    # open+close at T0 (ancient)
    s = step(s, FAIL, th, now=T0).state
    s = step(s, OK, th, now=T0 + 1).state
    # open+close at T0 + 1000 (outside the 600s window from the next open)
    s = step(s, FAIL, th, now=T0 + 1000).state
    s = step(s, OK, th, now=T0 + 1001).state
    # open at T0 + 1002 — only recent_opens within 600s count; the T0 one is
    # pruned, so this is only the 2nd recent open → normal OPEN, not FLAP.
    t = step(s, FAIL, th, now=T0 + 1002)
    assert t.action is Action.OPEN_INCIDENT
    assert t.state.status == "down"


# --- Clock injection: the function never reads a wall clock ---


def test_clock_injected_not_wall_clock() -> None:
    """Flapping detection depends on the injected `now`, not real time."""
    th = thresholds(failure_threshold=1, recovery_threshold=1, flap_opens=3, flap_window_s=600.0)
    s = MonitorState()
    # Three opens spaced exactly within window via injected now.
    for i in range(2):
        s = step(s, FAIL, th, now=T0 + i * 10).state
        s = step(s, OK, th, now=T0 + i * 10 + 1).state
    t = step(s, FAIL, th, now=T0 + 20)
    assert t.action is Action.FLAP_START  # 3rd open in window


# --- Mute is NOT a state-machine concern ---


def test_state_machine_is_mute_agnostic() -> None:
    """The transition function takes no mute flag and its output is unchanged.

    Mute is a delivery-layer concern: a muted Monitor still opens/closes
    Incidents normally. This test asserts the SM has no mute parameter and that
    a failing streak opens an Incident regardless of any external mute concept.
    """
    import inspect

    from tgmonitor.incidents import step as step_fn

    params = inspect.signature(step_fn).parameters
    assert "muted" not in params, "the state machine must not take a mute flag"
    assert "mute" not in params, "the state machine must not take a mute flag"

    # Behaviour: an Incident opens on N failures — mute (if it existed) wouldn't
    # change that. We assert the open happens.
    s = MonitorState()
    th = thresholds(failure_threshold=2)
    s = step(s, FAIL, th, now=T0).state
    t = step(s, FAIL, th, now=T0)
    assert t.action is Action.OPEN_INCIDENT
