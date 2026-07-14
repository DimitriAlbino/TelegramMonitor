"""The Incident state machine — the pure, decision-dense core (ADR-0005, Seam A).

This is the highest-value pure seam in the system. The transition function takes
the current monitor state, the latest Check Result, thresholds, and an *injected*
clock + recent-opens window — it never reads a wall clock — and returns the new
state plus an action (open / close / flap / none). Because it is pure, it is
heavily unit-tested without DB, network, or time.

Mute is deliberately NOT here. Mute is a delivery-layer flag: a muted Monitor
still opens and closes Incidents normally; mute suppresses only the Alert send.
A test asserts this module is mute-agnostic.

CONTEXT.md:
    Incident — A contiguous span during which a Monitor was in a failing state,
    opened when failures cross a threshold and closed on recovery.

State model (per Monitor):
    - status: "ok" | "down" | "flapping"
    - consecutive_failures, consecutive_successes: debounce counters
    - open_count_window: list of injected timestamps of recent Incident opens
      (used to detect flapping; the caller supplies this from the DB/persisted
      record so the function stays pure).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Literal


class Action(StrEnum):
    """What the state machine wants the surrounding system to do."""

    NONE = "none"
    OPEN_INCIDENT = "open_incident"
    CLOSE_INCIDENT = "close_incident"
    FLAP_START = "flap_start"
    FLAP_END = "flap_end"


# A Check outcome as seen by the state machine — just success/failure + a reason.
@dataclass(frozen=True, slots=True)
class CheckOutcome:
    success: bool
    reason: str = ""


@dataclass(slots=True)
class MonitorState:
    """The mutable state of one Monitor's incident tracking.

    The counters and status live here (not in the DB row's incidental columns)
    so the pure function is the only thing that mutates them. The engine loads
    this from the Monitor row, calls :func:`step`, and writes it back.
    """

    status: Literal["ok", "down", "flapping"] = "ok"
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    # Timestamps (epoch seconds, injected) of recent Incident opens within the
    # flapping window. The caller prunes this to the window before calling step.
    recent_opens: list[float] = field(default_factory=list)

    # When status == "down", the id of the open Incident (so the engine can
    # close it). Set by the engine after it persists the open; not the SM's job.
    open_incident_id: int | None = None


@dataclass(frozen=True, slots=True)
class Thresholds:
    failure_threshold: int = 2
    recovery_threshold: int = 2
    # Flapping: this many opens within flap_window_s enters flapping.
    flap_opens: int = 3
    flap_window_s: float = 600.0  # 10 minutes
    # Stabilization: this many consecutive successes exits flapping.
    flap_stabilization: int = 5


@dataclass(frozen=True, slots=True)
class Transition:
    state: MonitorState
    action: Action
    reason: str = ""


def step(
    state: MonitorState,
    result: CheckOutcome,
    thresholds: Thresholds,
    *,
    now: float,
) -> Transition:
    """Advance the state machine by one Check Result.

    Pure: ``now`` is injected (epoch seconds). Returns the new state (a fresh
    copy via replace, so the caller's state is not mutated) and an Action.

    Rules (ADR-0005):
    - A failing result increments consecutive_failures; reaching
      failure_threshold opens an Incident (action OPEN_INCIDENT), unless the
      monitor is flapping (then suppressed).
    - A successful result increments consecutive_successes; reaching
      recovery_threshold closes an open Incident (action CLOSE_INCIDENT) or ends
      flapping (action FLAP_END).
    - A success mid-failure-streak resets the failure counter.
    - Flapping: when the count of recent opens within flap_window_s reaches
      flap_opens, the monitor enters "flapping" (action FLAP_START). No further
      opens while flapping. After flap_stabilization consecutive successes, a
      single FLAP_END recovery.
    """
    # Work on a copy so the caller's state object is untouched.
    s = replace(
        state,
        recent_opens=list(state.recent_opens),
    )

    # --- Flapping stabilization takes priority on success ---
    if s.status == "flapping":
        if result.success:
            s.consecutive_successes += 1
            s.consecutive_failures = 0
            if s.consecutive_successes >= thresholds.flap_stabilization:
                s.status = "ok"
                s.consecutive_successes = 0
                s.recent_opens = []
                return Transition(s, Action.FLAP_END, "flapping resolved")
        else:
            # A failure during flapping resets stabilization progress but does
            # not open a new Incident (suppressed).
            s.consecutive_successes = 0
        return Transition(s, Action.NONE)

    # --- Not flapping: normal debounce logic ---
    if not result.success:
        s.consecutive_failures += 1
        s.consecutive_successes = 0
        if s.status == "ok" and s.consecutive_failures >= thresholds.failure_threshold:
            # Would open an Incident — but check flapping first.
            opens_in_window = [t for t in s.recent_opens if now - t <= thresholds.flap_window_s]
            opens_in_window.append(now)
            if len(opens_in_window) >= thresholds.flap_opens:
                s.status = "flapping"
                s.consecutive_failures = 0
                s.consecutive_successes = 0
                s.recent_opens = opens_in_window
                return Transition(s, Action.FLAP_START, "flapping detected")
            # Open the Incident.
            s.status = "down"
            s.recent_opens = opens_in_window
            s.consecutive_failures = 0  # reset after opening
            return Transition(s, Action.OPEN_INCIDENT, result.reason or "threshold reached")
        return Transition(s, Action.NONE)

    # --- Success ---
    s.consecutive_successes += 1
    s.consecutive_failures = 0
    if s.status == "down" and s.consecutive_successes >= thresholds.recovery_threshold:
        s.status = "ok"
        s.consecutive_successes = 0
        s.open_incident_id = None
        return Transition(s, Action.CLOSE_INCIDENT, "recovered")
    return Transition(s, Action.NONE)


def prune_recent_opens(
    recent_opens: Sequence[float], *, now: float, window_s: float
) -> list[float]:
    """Helper for callers: keep only opens within the flapping window."""
    return [t for t in recent_opens if now - t <= window_s]
