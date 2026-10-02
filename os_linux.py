"""
Linux adapter for Flow's OS seam (see issue #4). Same interface as os_win.py:

  hotkey(spec, on_press, on_release) → bool   register/re-register the global hotkey
  paste(text, handle)                          type text into the window `handle`
  foreground() → (handle, app_name)            the window focused right now

Every desktop: bind `flow-studio toggle` (POSTs /api/toggle) to a desktop keyboard
shortcut. On X11 the hotkey is also grabbed here (libX11 via ctypes), which gives
hold-to-talk. Wayland gives apps no global keys or focus info, so there hotkey()
reports False and foreground() is (None, "app").
Paste chain: clipboard (xclip / wl-copy) + Ctrl+V sent by xdotool (X11) or ydotool;
with neither, the text stays on the clipboard and the user is told to press Ctrl+V.
"""

import ctypes
import ctypes.util
import os
import shutil
import subprocess
import threading
import time


def _x11():
    """True when windows are X11 ones we can see and drive (also WSLg, which sets no session type)."""
    return os.environ.get("XDG_SESSION_TYPE") != "wayland" and bool(os.environ.get("DISPLAY"))


def _run(*cmd, **kw):
    return subprocess.run(cmd, timeout=2, **kw)


# ── focused window ──────────────────────────────────────────────────────────
def foreground():
    """(X window id, short title) of the focused window; (None, "app") on Wayland or without xdotool."""
    if not (_x11() and shutil.which("xdotool")):
        return None, "app"
    try:
        wid = _run("xdotool", "getactivewindow", capture_output=True, text=True, check=True).stdout.strip()
        title = _run("xdotool", "getwindowname", wid, capture_output=True, text=True).stdout.strip()
        return int(wid), (title.split(" - ")[-1][:24] or "app")
    except Exception:
        return None, "app"


# ── typing into the focused app ─────────────────────────────────────────────
NO_KEYS = ("Your text is on the clipboard: press Ctrl+V. "
           "To paste automatically, install xdotool (X11) or ydotool (Wayland).")
NO_CLIP = "Flow Studio can't reach the clipboard: install xclip (X11) or wl-clipboard (Wayland)."


def _clip_tool():
    """(copy, read) commands of the first clipboard tool found, the session's native one first."""
    xclip = (["xclip", "-selection", "clipboard"], ["xclip", "-selection", "clipboard", "-o"])
    wl = (["wl-copy"], ["wl-paste", "--no-newline"])
    return next((t for t in ((xclip, wl) if _x11() else (wl, xclip)) if shutil.which(t[0][0])), None)


def _clip_get():
    tool = _clip_tool()
    try:
        r = _run(*tool[1], capture_output=True, text=True)
        return r.stdout if r.returncode == 0 else None
    except Exception:
        return None


def _clip_set(text):
    tool = _clip_tool()
    try:
        # xclip / wl-copy fork to serve the clipboard: give them no pipes they could hold open
        return _run(*tool[0], input=text, text=True, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL).returncode == 0
    except Exception:
        return False


def paste(text, handle=None):
    """Insert text at the cursor via clipboard + Ctrl+V, restoring the previous clipboard.
    Without a key-sending tool the text stays on the clipboard and PermissionError
    (with a user-facing message) is raised, as on the Mac."""
    if not text:
        return
    time.sleep(0.2)  # let the hotkey keys release
    prev = _clip_get()
    if not _clip_set(text):
        if _x11() and shutil.which("xdotool"):  # no clipboard tool: type it out instead
            _run("xdotool", "type", "--clearmodifiers", "--", text, timeout=30, check=True)
            return
        raise RuntimeError(NO_CLIP)
    if _x11() and shutil.which("xdotool"):
        if handle:  # put focus back on the window the user started in
            _run("xdotool", "windowactivate", "--sync", str(handle))
        _run("xdotool", "key", "--clearmodifiers", "ctrl+v", check=True)
    elif shutil.which("ydotool"):
        _run("ydotool", "key", "29:1", "47:1", "47:0", "29:0", check=True)  # KEY_LEFTCTRL, KEY_V
    else:
        raise PermissionError(NO_KEYS)
    time.sleep(0.18)
    if prev:
        _clip_set(prev)  # put the user's clipboard back


# ── global hotkey via an X11 key grab ───────────────────────────────────────
_MOD = {"shift": 1, "ctrl": 4, "control": 4, "alt": 8, "super": 64, "win": 64, "windows": 64,
        "meta": 64, "cmd": 64}
_LOCKS = (0, 2, 16, 18)  # also grab with CapsLock / NumLock on, or the combo never fires
_KEYSYM = {"space": "space", "enter": "Return", "return": "Return", "tab": "Tab", "esc": "Escape",
           "escape": "Escape", "backspace": "BackSpace", "delete": "Delete", "up": "Up",
           "down": "Down", "left": "Left", "right": "Right", "home": "Home", "end": "End",
           "pageup": "Prior", "pagedown": "Next", "insert": "Insert"}
_KEYSYM.update({f"f{i}": f"F{i}" for i in range(1, 13)})


def _parse_hotkey(s):
    """'ctrl+shift+space' → (X modifier mask, keysym name) ; keysym None if no main key."""
    mods, ks = 0, None
    for p in (s or "").lower().replace(" ", "").split("+"):
        if p in _MOD:
            mods |= _MOD[p]
        elif p in _KEYSYM:
            ks = _KEYSYM[p]
        elif len(p) == 1 and p.isalnum():
            ks = p
    return mods, ks


_ERR_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
_grab_failed = False


@_ERR_HANDLER
def _on_x_error(_dpy, _ev):
    global _grab_failed
    _grab_failed = True  # BadAccess: another app holds the combo
    return 0


_hk_new = None
_hk_lock = threading.Lock()
_hk_applied = threading.Event()
_hk_thread = None
_hk_ok = False
_callbacks = (None, None)


def hotkey(spec, on_press, on_release):
    """Register `spec` as the global hotkey, replacing the previous one. on_press
    fires on key-down, on_release on key-up (both on the hotkey thread). Returns True
    if the X server granted the grab; False on Wayland. Raises ValueError, leaving
    the current hotkey untouched, if `spec` has no normal key."""
    global _hk_new, _hk_thread, _callbacks
    if _parse_hotkey(spec)[1] is None:
        raise ValueError(f"'{spec}' has no normal key (modifier-only unsupported)")
    # ponytail: Wayland has no X grab; the GlobalShortcuts portal (KDE, GNOME 48+) would add hold-to-talk there
    if not _x11() or not ctypes.util.find_library("X11"):
        return False
    _callbacks = (on_press, on_release)
    _hk_applied.clear()
    with _hk_lock:
        _hk_new = spec
    if _hk_thread is None or not _hk_thread.is_alive():
        _hk_thread = threading.Thread(target=_hotkey_loop, daemon=True)
        _hk_thread.start()
    _hk_applied.wait(1.0)
    return _hk_ok


def _xlib():
    x = ctypes.CDLL(ctypes.util.find_library("X11"))
    x.XOpenDisplay.restype = ctypes.c_void_p
    x.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x.XDefaultRootWindow.restype = ctypes.c_ulong
    x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    x.XStringToKeysym.restype = ctypes.c_ulong
    x.XStringToKeysym.argtypes = [ctypes.c_char_p]
    x.XKeysymToKeycode.restype = ctypes.c_ubyte
    x.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    x.XGrabKey.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_ulong,
                           ctypes.c_int, ctypes.c_int, ctypes.c_int]
    x.XUngrabKey.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_ulong]
    x.XSetErrorHandler.restype = ctypes.c_void_p
    x.XSetErrorHandler.argtypes = [ctypes.c_void_p]
    x.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    x.XPending.argtypes = [ctypes.c_void_p]
    x.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    x.XkbSetDetectableAutoRepeat.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    return x


def _hotkey_loop():
    global _hk_new, _hk_ok, _grab_failed
    try:
        x = _xlib()
        dpy = x.XOpenDisplay(None)  # our own connection, used only on this thread
    except Exception:
        dpy = None
    if not dpy:
        _hk_ok = False
        _hk_applied.set()
        return
    root = x.XDefaultRootWindow(dpy)
    x.XkbSetDetectableAutoRepeat(dpy, 1, None)  # held key → one press, one release
    ev = ctypes.create_string_buffer(192)  # sizeof(XEvent)
    grabbed = None
    down = False
    while True:
        with _hk_lock:
            pending, _hk_new = _hk_new, None
        if pending:
            if grabbed:
                x.XUngrabKey(dpy, grabbed, 1 << 15, root)  # AnyModifier
                grabbed = None
            mods, ks = _parse_hotkey(pending)
            kc = x.XKeysymToKeycode(dpy, x.XStringToKeysym(ks.encode()))
            if kc:
                # The error handler is process-wide (GTK has one too): swap ours in only around the grab.
                _grab_failed = False
                old = x.XSetErrorHandler(ctypes.cast(_on_x_error, ctypes.c_void_p))
                for lock in _LOCKS:
                    x.XGrabKey(dpy, kc, mods | lock, root, 1, 1, 1)  # GrabModeAsync
                x.XSync(dpy, 0)
                x.XSetErrorHandler(old)
                if _grab_failed:
                    x.XUngrabKey(dpy, kc, 1 << 15, root)
                else:
                    grabbed = kc
            _hk_ok = bool(grabbed)
            _hk_applied.set()

        while x.XPending(dpy):
            x.XNextEvent(dpy, ev)
            kind = ctypes.c_int.from_buffer(ev).value  # XEvent.type
            try:
                if kind == 2 and not down:    # KeyPress
                    down = True
                    _callbacks[0]()
                elif kind == 3 and down:      # KeyRelease
                    down = False
                    _callbacks[1]()
            except Exception:
                pass  # a broken callback must not kill the hotkey thread
        time.sleep(0.015)
