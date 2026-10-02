"""os_mac.py logic that runs off the Mac: hotkey parsing and the no-Accessibility paste path
(pyobjc is stubbed). Needs numpy + flask for the flow part."""

import sys
import types

import pytest

import os_mac


def test_parse_hotkey():
    assert os_mac._parse_hotkey("cmd+shift+space") == (0x0100 | 0x0200, 49)
    assert os_mac._parse_hotkey("ctrl+alt+a") == (0x1000 | 0x0800, 0)
    assert os_mac._parse_hotkey("cmd+f5") == (0x0100, 96)
    assert os_mac._parse_hotkey("cmd+shift")[1] is None


def test_modifier_only_hotkey_raises():
    with pytest.raises(ValueError):
        os_mac.hotkey("cmd+shift", None, None)


@pytest.fixture
def mac(monkeypatch):
    clip = {"text": "user's clipboard"}
    posted = []

    class Pasteboard:
        def stringForType_(self, _t): return clip["text"]
        def clearContents(self): clip["text"] = None
        def setString_forType_(self, s, _t): clip["text"] = s

    pb = Pasteboard()
    appkit = types.SimpleNamespace(
        NSPasteboard=types.SimpleNamespace(generalPasteboard=lambda: pb),
        NSPasteboardTypeString="public.utf8-plain-text",
        NSRunningApplication=types.SimpleNamespace(runningApplicationWithProcessIdentifier_=lambda pid: None),
        NSApplicationActivateIgnoringOtherApps=2)
    access = {"ok": False, "requests": 0}
    quartz = types.SimpleNamespace(
        CGPreflightPostEventAccess=lambda: access["ok"],
        CGRequestPostEventAccess=lambda: access.__setitem__("requests", access["requests"] + 1),
        CGEventCreateKeyboardEvent=lambda src, kc, down: (kc, down),
        CGEventSetFlags=lambda ev, f: None,
        CGEventPost=lambda tap, ev: posted.append(ev),
        kCGEventFlagMaskCommand=1 << 20, kCGHIDEventTap=0)
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    monkeypatch.setattr(os_mac.time, "sleep", lambda s: None)
    monkeypatch.setattr(os_mac, "_prompted", False)
    return clip, posted, access


def test_paste_without_accessibility_leaves_text_on_clipboard(mac):
    clip, posted, access = mac
    for _ in range(2):
        with pytest.raises(PermissionError, match="Accessibility"):
            os_mac.paste("hello", 123)
    assert clip["text"] == "hello" and posted == []
    assert access["requests"] == 1  # system prompt shown once, not every dictation


def test_paste_sends_cmd_v_and_restores_clipboard(mac):
    clip, posted, access = mac
    access["ok"] = True
    os_mac.paste("hello", 123)
    assert posted == [(9, True), (9, False)]
    assert clip["text"] == "user's clipboard"


def test_flow_surfaces_paste_permission_error(monkeypatch):
    import flow

    def denied(text, handle):
        raise PermissionError(os_mac.NO_ACCESS)
    monkeypatch.setattr(flow.osi, "paste", denied)
    monkeypatch.setattr(flow, "cleanup", lambda raw: raw)
    added = []
    monkeypatch.setattr(flow, "add_history", lambda text, app: added.append(text))
    for k, v in dict(app="TextEdit", status="transcribing", error="", raw="", clean="").items():
        monkeypatch.setitem(flow.state, k, v)
    flow._finalize("hello")
    assert flow.state["status"] == "idle" and flow.state["error"] == os_mac.NO_ACCESS
    assert added == ["hello"]
