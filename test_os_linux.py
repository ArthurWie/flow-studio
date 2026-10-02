"""os_linux.py paste chain, hotkey parsing and the toggle endpoint, with the desktop tools
stubbed. Needs numpy + flask for the flow part."""

import pytest

import os_linux


def test_parse_hotkey():
    assert os_linux._parse_hotkey("ctrl+shift+space") == (4 | 1, "space")
    assert os_linux._parse_hotkey("super+alt+f5") == (64 | 8, "F5")
    assert os_linux._parse_hotkey("ctrl+a") == (4, "a")
    assert os_linux._parse_hotkey("ctrl+shift")[1] is None


def test_modifier_only_hotkey_raises():
    with pytest.raises(ValueError):
        os_linux.hotkey("ctrl+shift", None, None)


def test_wayland_has_no_hotkey_or_foreground(monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("DISPLAY", ":0")
    assert os_linux.hotkey("ctrl+shift+space", None, None) is False
    assert os_linux.foreground() == (None, "app")


@pytest.fixture
def desk(monkeypatch):
    """A fake desktop: `tools` are installed, the clipboard is a dict, commands are recorded."""
    d = {"tools": set(), "clip": "user's clipboard", "ran": []}

    def run(*cmd, input=None, **_kw):
        d["ran"].append(cmd)
        if cmd[0] in ("wl-copy", "xclip") and "-o" not in cmd:
            d["clip"] = input
        return type("R", (), {"returncode": 0, "stdout": d["clip"]})()

    monkeypatch.setattr(os_linux, "_run", run)
    monkeypatch.setattr(os_linux.shutil, "which", lambda t: t if t in d["tools"] else None)
    monkeypatch.setattr(os_linux.time, "sleep", lambda s: None)
    return d


def _session(monkeypatch, kind):
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("XDG_SESSION_TYPE", kind)


def test_x11_pastes_with_xdotool_and_restores_clipboard(monkeypatch, desk):
    _session(monkeypatch, "x11")
    desk["tools"] |= {"xclip", "wl-copy", "xdotool"}
    os_linux.paste("hello", 0x1234)
    sent = [c for c in desk["ran"] if c[0] == "xdotool"]
    assert sent == [("xdotool", "windowactivate", "--sync", "4660"),
                    ("xdotool", "key", "--clearmodifiers", "ctrl+v")]
    assert all(c[0] != "wl-copy" for c in desk["ran"])  # X11 uses xclip first
    assert desk["clip"] == "user's clipboard"


def test_x11_without_clipboard_tool_types(monkeypatch, desk):
    _session(monkeypatch, "x11")
    desk["tools"] |= {"xdotool"}
    os_linux.paste("hello")
    assert desk["ran"][-1] == ("xdotool", "type", "--clearmodifiers", "--", "hello")


def test_wayland_pastes_with_ydotool(monkeypatch, desk):
    _session(monkeypatch, "wayland")
    desk["tools"] |= {"wl-copy", "xclip", "xdotool", "ydotool"}
    os_linux.paste("hello")
    assert ("ydotool", "key", "29:1", "47:1", "47:0", "29:0") in desk["ran"]
    assert all(c[0] not in ("xdotool", "xclip") for c in desk["ran"])


def test_wayland_without_ydotool_leaves_text_on_clipboard(monkeypatch, desk):
    _session(monkeypatch, "wayland")
    desk["tools"] |= {"wl-copy"}
    with pytest.raises(PermissionError, match="Ctrl\\+V"):
        os_linux.paste("hello")
    assert desk["clip"] == "hello"


def test_no_clipboard_no_xdotool_says_what_to_install(monkeypatch, desk):
    _session(monkeypatch, "wayland")
    with pytest.raises(RuntimeError, match="wl-clipboard"):
        os_linux.paste("hello")


def test_toggle_starts_then_stops(monkeypatch):
    import flow
    calls = []
    monkeypatch.setitem(flow.state, "status", "idle")
    monkeypatch.setattr(flow, "do_start", lambda: (calls.append("start"), flow.state.update(status="recording")))
    monkeypatch.setattr(flow, "do_stop", lambda: (calls.append("stop"), flow.state.update(status="transcribing")))
    c = flow.app.test_client()
    assert c.post("/api/toggle").json["status"] == "recording"
    assert c.post("/api/toggle").json["status"] == "transcribing"
    assert calls == ["start", "stop"]
