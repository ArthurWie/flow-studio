"""
macOS adapter for Flow's OS seam (see issue #4). Same interface as os_win.py:

  hotkey(spec, on_press, on_release) → bool   register/re-register the global hotkey
  paste(text, handle)                          type text into the app `handle` (a pid)
  foreground() → (handle, app_name)            the app focused right now
  signature_ok(path) → bool                    a downloaded app is validly signed

The hotkey uses Carbon RegisterEventHotKey: it reports press and release and needs
no Input Monitoring permission. Its events arrive on the main thread's Cocoa run
loop, which pywebview (or AppHelper.runEventLoop in `python flow.py`) provides.
Pasting posts Cmd+V with CGEvent, which needs the Accessibility permission.
pyobjc (AppKit, Quartz) comes with pywebview on the Mac; imports are lazy so this
module can be imported (and its parser tested) anywhere.
"""

import ctypes
import threading
import time


# ── focused app ─────────────────────────────────────────────────────────────
def foreground():
    """(pid, app name) of the frontmost app (the field to paste into)."""
    try:
        from AppKit import NSWorkspace
        a = NSWorkspace.sharedWorkspace().frontmostApplication()
        return int(a.processIdentifier()), (a.localizedName() or "app")[:24]
    except Exception:
        return None, "app"


# ── typing into the focused app ─────────────────────────────────────────────
_KC_V = 9
_prompted = False
NO_ACCESS = ("Flow Studio needs the Accessibility permission to paste for you "
             "(System Settings → Privacy & Security → Accessibility). "
             "Your text is on the clipboard: press Cmd+V.")


def _can_post_keys():
    """True if we may send keystrokes. Shows the system's Accessibility prompt once per run."""
    global _prompted
    import Quartz
    if Quartz.CGPreflightPostEventAccess():
        return True
    if not _prompted:
        _prompted = True
        Quartz.CGRequestPostEventAccess()  # opens the system prompt / Settings pane
    return False


def paste(text, handle=None):
    """Insert text at the cursor via clipboard + Cmd+V, restoring the previous clipboard.
    Without the Accessibility permission the text stays on the clipboard and
    PermissionError (with a user-facing message) is raised."""
    if not text:
        return
    import Quartz
    from AppKit import (NSApplicationActivateIgnoringOtherApps, NSPasteboard,
                        NSPasteboardTypeString, NSRunningApplication)
    time.sleep(0.2)  # let the hotkey keys release
    pb = NSPasteboard.generalPasteboard()
    prev = pb.stringForType_(NSPasteboardTypeString)
    pb.clearContents()
    pb.setString_forType_(text, NSPasteboardTypeString)
    if not _can_post_keys():
        raise PermissionError(NO_ACCESS)
    # Put focus back on the app the user started in, in case the overlay stole it.
    if handle:
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(handle)
        if app:
            app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
            time.sleep(0.06)
    for down in (True, False):
        ev = Quartz.CGEventCreateKeyboardEvent(None, _KC_V, down)
        Quartz.CGEventSetFlags(ev, Quartz.kCGEventFlagMaskCommand)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
    time.sleep(0.18)
    if prev:
        pb.clearContents()
        pb.setString_forType_(prev, NSPasteboardTypeString)  # put the user's clipboard back


# ── global hotkey via Carbon RegisterEventHotKey ────────────────────────────
_MOD = {"cmd": 0x0100, "command": 0x0100, "shift": 0x0200, "alt": 0x0800,
        "option": 0x0800, "ctrl": 0x1000, "control": 0x1000}
# kVK_* virtual key codes (ANSI layout) from HIToolbox/Events.h
_VK = {"space": 49, "enter": 36, "return": 36, "tab": 48, "esc": 53, "escape": 53,
       "backspace": 51, "delete": 117, "up": 126, "down": 125, "left": 123,
       "right": 124, "home": 115, "end": 119, "pageup": 116, "pagedown": 121}
_VK.update({"a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8,
            "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
            "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "9": 25, "7": 26,
            "8": 28, "0": 29, "o": 31, "u": 32, "i": 34, "p": 35, "l": 37, "j": 38,
            "k": 40, "n": 45, "m": 46})
_VK.update(zip((f"f{i}" for i in range(1, 13)),
               (122, 120, 99, 118, 96, 97, 98, 100, 101, 109, 103, 111)))

_K_EVENT_CLASS_KEYBOARD = int.from_bytes(b"keyb", "big")
_K_HOTKEY_PRESSED, _K_HOTKEY_RELEASED = 5, 6


def _parse_hotkey(s):
    """'cmd+shift+space' → (carbon_modifiers, key_code) ; key_code None if no main key."""
    mods, kc = 0, None
    for p in (s or "").lower().replace(" ", "").split("+"):
        if p in _MOD:
            mods |= _MOD[p]
        elif p in _VK:
            kc = _VK[p]
    return mods, kc


class _HotKeyID(ctypes.Structure):
    _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]


class _EventTypeSpec(ctypes.Structure):
    _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]


_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
_carbon = None
_handler = None           # keep the ctypes callback alive
_hk_ref = ctypes.c_void_p()
_callbacks = (None, None)


def _on_event(_call_ref, event, _user):
    kind = _carbon.GetEventKind(event)
    cb = _callbacks[0] if kind == _K_HOTKEY_PRESSED else _callbacks[1]
    try:
        cb()
    except Exception:
        pass  # never let an exception unwind into Carbon
    return 0  # noErr


def _register(mods, kc):
    """Main thread only. Installs the event handler once, then swaps the hotkey."""
    global _carbon, _handler
    if _carbon is None:
        c = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
        c.GetApplicationEventTarget.restype = ctypes.c_void_p
        c.GetEventKind.restype = ctypes.c_uint32
        c.GetEventKind.argtypes = [ctypes.c_void_p]
        c.InstallEventHandler.argtypes = [ctypes.c_void_p, _HANDLER, ctypes.c_ulong,
                                          ctypes.POINTER(_EventTypeSpec), ctypes.c_void_p,
                                          ctypes.c_void_p]
        c.RegisterEventHotKey.argtypes = [ctypes.c_uint32, ctypes.c_uint32, _HotKeyID,
                                          ctypes.c_void_p, ctypes.c_uint32,
                                          ctypes.POINTER(ctypes.c_void_p)]
        c.UnregisterEventHotKey.argtypes = [ctypes.c_void_p]
        specs = (_EventTypeSpec * 2)((_K_EVENT_CLASS_KEYBOARD, _K_HOTKEY_PRESSED),
                                     (_K_EVENT_CLASS_KEYBOARD, _K_HOTKEY_RELEASED))
        _handler = _HANDLER(_on_event)
        if c.InstallEventHandler(c.GetApplicationEventTarget(), _handler, 2, specs, None, None):
            return False
        _carbon = c
    if _hk_ref.value:
        _carbon.UnregisterEventHotKey(_hk_ref)
        _hk_ref.value = None
    hid = _HotKeyID(int.from_bytes(b"FLOW", "big"), 1)
    return _carbon.RegisterEventHotKey(kc, mods, hid, _carbon.GetApplicationEventTarget(),
                                       0, ctypes.byref(_hk_ref)) == 0


def hotkey(spec, on_press, on_release):
    """Register `spec` as the global hotkey, replacing the previous one. on_press
    fires on key-down, on_release on key-up (both on the main thread). Returns True
    if macOS accepted the combo. Raises ValueError, leaving the current hotkey
    untouched, if `spec` has no normal key."""
    global _callbacks
    mods, kc = _parse_hotkey(spec)
    if kc is None:
        raise ValueError(f"'{spec}' has no normal key (modifier-only unsupported)")
    _callbacks = (on_press, on_release)
    if threading.current_thread() is threading.main_thread():
        return _register(mods, kc)
    from PyObjCTools import AppHelper
    out, done = [False], threading.Event()

    def run():
        try:
            out[0] = _register(mods, kc)
        finally:
            done.set()
    AppHelper.callAfter(run)  # Carbon wants the main thread
    done.wait(5.0)  # ponytail: reports False if the main run loop isn't up within 5 s
    return out[0]


# ── downloaded-app check ────────────────────────────────────────────────────
def signature_ok(path):
    """True only if the app's signature is intact and chains to Apple (a Developer ID;
    ad-hoc and self-signed fail): the codesign counterpart of os_win's Authenticode check."""
    import subprocess
    try:
        return subprocess.run(["codesign", "--verify", "--deep", "--strict", "-R=anchor apple generic", str(path)],
                              capture_output=True, timeout=120).returncode == 0
    except Exception:
        return False
