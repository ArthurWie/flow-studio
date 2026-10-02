"""The Mac overlay must never activate Flow Studio: it lives in a non-activating NSPanel
that shows with orderFrontRegardless. AppKit is stubbed, so this runs anywhere."""

import sys
import types

import flow_studio


class Panel:
    def __init__(self, calls): self.calls = calls
    def initWithContentRect_styleMask_backing_defer_(self, rect, mask, backing, defer):
        self.calls.append(("mask", mask)); return self
    def __getattr__(self, name):  # setLevel_, orderFrontRegardless, … just record the call
        return lambda *a: self.calls.append((name, *a))


def test_mac_overlay_is_a_non_activating_panel(monkeypatch):
    calls = []
    frame = types.SimpleNamespace(origin=types.SimpleNamespace(x=0, y=0),
                                  size=types.SimpleNamespace(width=1000, height=800))
    appkit = types.SimpleNamespace(
        NSPanel=types.SimpleNamespace(alloc=lambda: Panel(calls)),
        NSScreen=types.SimpleNamespace(mainScreen=lambda: types.SimpleNamespace(visibleFrame=lambda: frame)),
        NSMakeRect=lambda *r: r, NSColor=types.SimpleNamespace(clearColor=lambda: "clear"),
        NSWindowStyleMaskBorderless=0, NSWindowStyleMaskNonactivatingPanel=1 << 7,
        NSBackingStoreBuffered=2, NSStatusWindowLevel=25,
        NSWindowCollectionBehaviorCanJoinAllSpaces=1, NSWindowCollectionBehaviorFullScreenAuxiliary=256)
    helper = types.SimpleNamespace(callAfter=lambda f, *a: f(*a))
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    monkeypatch.setitem(sys.modules, "PyObjCTools", types.SimpleNamespace(AppHelper=helper))
    monkeypatch.setitem(sys.modules, "PyObjCTools.AppHelper", helper)
    overlay = types.SimpleNamespace(
        events=types.SimpleNamespace(loaded=types.SimpleNamespace(wait=lambda t: True)),
        native=types.SimpleNamespace(contentView=lambda: "webview"))

    show, hide = flow_studio._mac_overlay(overlay)
    assert ("mask", 1 << 7) in calls
    assert ("setHidesOnDeactivate_", False) in calls and ("setContentView_", "webview") in calls
    calls.clear()
    show(); hide()
    assert calls == [("orderFrontRegardless",), ("orderOut_", None)]
