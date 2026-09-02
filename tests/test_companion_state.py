from pet.companion_state import (
    CompanionState,
    DESKTOP_SURFACE,
    STABLE,
    TO_CONNECTOR,
    connector_surface,
)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_starts_on_desktop_with_canonical_identity():
    state = CompanionState(instance_id="test-instance")
    snap = state.snapshot(now=0.0)
    assert snap["petId"] == "shenshen"
    assert snap["instanceId"] == "test-instance"
    assert snap["activeSurface"] == DESKTOP_SURFACE
    assert snap["transition"] == STABLE


def test_connector_heartbeat_does_not_steal_visual_ownership():
    clock = Clock()
    state = CompanionState(clock=clock)
    state.heartbeat("dsh-agent-office", "lease-a")
    assert state.snapshot()["activeSurface"] == DESKTOP_SURFACE
    assert state.snapshot()["revision"] == 0


def test_two_phase_handoff_and_duplicate_request_are_idempotent():
    clock = Clock()
    state = CompanionState(clock=clock)
    state.heartbeat("dsh-agent-office", "lease-a")
    target = connector_surface("dsh-agent-office")

    requested, accepted = state.request_handoff("h-1", target, connector_id="dsh-agent-office")
    assert accepted is True
    assert requested["activeSurface"] == DESKTOP_SURFACE
    assert requested["transition"] == TO_CONNECTOR

    duplicate, accepted = state.request_handoff("h-1", target, connector_id="dsh-agent-office")
    assert accepted is False
    assert duplicate["revision"] == requested["revision"]

    committed, changed = state.commit_handoff("h-1")
    assert changed is True
    assert committed["activeSurface"] == target
    assert committed["transition"] == STABLE

    duplicate_commit, changed = state.commit_handoff("h-1")
    assert changed is False
    assert duplicate_commit["revision"] == committed["revision"]


def test_old_handoff_cannot_commit_a_new_transition():
    clock = Clock()
    state = CompanionState(clock=clock)
    state.heartbeat("dsh-agent-office", "lease-a")
    target = connector_surface("dsh-agent-office")
    state.request_handoff("new", target, connector_id="dsh-agent-office")
    snap, changed = state.commit_handoff("old")
    assert changed is False
    assert snap["activeSurface"] == DESKTOP_SURFACE
    assert snap["transition"] == TO_CONNECTOR


def test_handoff_timeout_rolls_back_to_source_surface():
    clock = Clock()
    state = CompanionState(clock=clock, handoff_timeout=5.0)
    state.heartbeat("dsh-agent-office", "lease-a")
    state.request_handoff(
        "h-timeout", connector_surface("dsh-agent-office"), connector_id="dsh-agent-office"
    )
    clock.now = 5.1
    event = state.expire()
    assert event["reason"] == "handoff_timeout"
    assert event["recoveredToDesktop"] is True
    assert event["state"]["activeSurface"] == DESKTOP_SURFACE


def test_connector_lease_expiry_recovers_to_desktop():
    clock = Clock()
    state = CompanionState(clock=clock, lease_timeout=8.0)
    state.heartbeat("dsh-agent-office", "lease-a")
    state.force_surface(
        connector_surface("dsh-agent-office"), connector_id="dsh-agent-office", reason="test"
    )
    clock.now = 8.1
    event = state.expire()
    assert event["reason"] == "connector_lease_expired"
    assert event["state"]["activeSurface"] == DESKTOP_SURFACE


def test_heartbeat_extends_active_connector_lease():
    clock = Clock()
    state = CompanionState(clock=clock, lease_timeout=8.0)
    state.heartbeat("dsh-agent-office", "lease-a")
    state.force_surface(
        connector_surface("dsh-agent-office"), connector_id="dsh-agent-office", reason="test"
    )
    clock.now = 6.0
    state.heartbeat("dsh-agent-office", "lease-a")
    clock.now = 10.0
    assert state.expire() is None
    assert state.snapshot()["activeSurface"] == connector_surface("dsh-agent-office")


def test_many_round_trips_never_create_multiple_active_surfaces():
    clock = Clock()
    state = CompanionState(clock=clock)
    connector = "dsh-agent-office"
    target = connector_surface(connector)
    for index in range(100):
        clock.now += 0.1
        state.heartbeat(connector, "lease-a")
        to_office = f"office-{index}"
        _, accepted = state.request_handoff(to_office, target, connector_id=connector)
        assert accepted is True
        snap, changed = state.commit_handoff(to_office)
        assert changed is True and snap["activeSurface"] == target

        to_desktop = f"desktop-{index}"
        _, accepted = state.request_handoff(to_desktop, DESKTOP_SURFACE, connector_id=connector)
        assert accepted is True
        snap, changed = state.commit_handoff(to_desktop)
        assert changed is True and snap["activeSurface"] == DESKTOP_SURFACE
        assert snap["transition"] == STABLE
