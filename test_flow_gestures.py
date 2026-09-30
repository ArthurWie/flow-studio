"""Hotkey gesture state machine in flow.py (hold / tap / double-tap), OS-independent.
Needs numpy + flask to import flow:  uv run --no-project --with numpy --with flask --with pytest pytest test_flow_gestures.py"""

import pytest

import flow


@pytest.fixture
def g(monkeypatch):
    clock = [100.0]
    calls = []

    def start():
        calls.append("start")
        flow.state["status"] = "recording"

    def stop():
        calls.append("stop")
        flow.state["status"] = "transcribing"

    monkeypatch.setattr(flow.time, "time", lambda: clock[0])
    monkeypatch.setattr(flow, "do_start", start)
    monkeypatch.setattr(flow, "do_stop", stop)
    monkeypatch.setitem(flow.state, "status", "idle")
    monkeypatch.setattr(flow, "_gesture", {"mode": "idle", "press_start": 0.0, "tap1": 0.0})

    def at(t, ev):
        clock[0] = 100.0 + t
        (flow._on_hotkey_press if ev == "down" else flow._on_hotkey_release)()
    return at, calls


def test_hold_then_release_confirms(g):
    at, calls = g
    at(0, "down"); at(0.5, "up")
    assert calls == ["start", "stop"]


def test_single_tap_is_handsfree_until_next_press(g):
    at, calls = g
    at(0, "down"); at(0.1, "up")
    assert calls == ["start"]          # still recording
    at(3.0, "down"); at(3.1, "up")     # press after the double-tap window ends it
    assert calls == ["start", "stop"]


def test_double_tap_is_handsfree_until_next_press(g):
    at, calls = g
    at(0, "down"); at(0.1, "up"); at(0.3, "down"); at(0.35, "up")
    assert calls == ["start"]
    at(2.0, "down")
    assert calls == ["start", "stop"]


def test_overlay_cancel_resets_gesture(g):
    at, calls = g
    at(0, "down"); at(0.1, "up")
    flow.state["status"] = "idle"      # ✕ in the overlay
    at(1.0, "down")                    # next press starts a fresh dictation
    assert calls == ["start", "start"]
