"""
Windows adapter for Flow's OS seam (see issue #4). Same interface on every OS:

  hotkey(spec, on_press, on_release) → bool   register/re-register the global hotkey
  paste(text, handle)                          type text into the window `handle`
  foreground() → (handle, app_name)            the window focused right now

Everything Win32 / ctypes lives here so flow.py stays portable.
"""

import ctypes
import threading
import time
from ctypes import wintypes


# ── focused window ──────────────────────────────────────────────────────────
def foreground():
    """(hwnd, short app name) of the window that's focused right now (the field to paste into)."""
    try:
        u = ctypes.windll.user32
        h = u.GetForegroundWindow()
        n = u.GetWindowTextLengthW(h)
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(h, buf, n + 1)
        title = (buf.value or "").strip()
        return int(h), (title.split(" - ")[-1][:24] or "app") if title else "app"
    except Exception:
        return 0, "app"


# ── typing into the focused app ─────────────────────────────────────────────
_CF_UNICODETEXT = 13


def _clip_get():
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalLock.argtypes = [ctypes.c_void_p]
    k.GlobalUnlock.argtypes = [ctypes.c_void_p]
    u.GetClipboardData.restype = ctypes.c_void_p
    if not u.OpenClipboard(0):
        return None
    try:
        h = u.GetClipboardData(_CF_UNICODETEXT)
        if not h:
            return ""
        p = k.GlobalLock(h)
        if not p:
            return ""
        txt = ctypes.wstring_at(p)
        k.GlobalUnlock(h)
        return txt
    finally:
        u.CloseClipboard()


def _clip_set(text):
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    k.GlobalAlloc.restype = ctypes.c_void_p
    k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalLock.argtypes = [ctypes.c_void_p]
    k.GlobalUnlock.argtypes = [ctypes.c_void_p]
    u.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    u.SetClipboardData.restype = ctypes.c_void_p
    data = text.encode("utf-16-le") + b"\x00\x00"
    if not u.OpenClipboard(0):
        return False
    try:
        u.EmptyClipboard()
        h = k.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
        p = k.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        k.GlobalUnlock(h)
        u.SetClipboardData(_CF_UNICODETEXT, h)  # system takes ownership of h
        return True
    finally:
        u.CloseClipboard()


def paste(text, handle=0):
    """Insert text at the cursor in the field the user was in — Wispr-Flow-style,
    via clipboard paste (reliable in any app), restoring the previous clipboard.
    If the clipboard path fails it falls back to typing, then re-raises for logging."""
    if not text:
        return
    time.sleep(0.2)  # let the hotkey keys release
    import keyboard
    # Put focus back on the field the user started in, in case the overlay stole it.
    if handle:
        try:
            ctypes.windll.user32.SetForegroundWindow(ctypes.c_void_p(handle))
            time.sleep(0.06)
        except Exception:
            pass
    try:
        prev = _clip_get()
        if _clip_set(text):
            keyboard.send("ctrl+v")
            time.sleep(0.18)
            if prev:
                _clip_set(prev)  # put the user's clipboard back
        else:
            keyboard.write(text)
    except Exception:
        try:
            keyboard.write(text)  # last-ditch fallback
        except Exception:
            pass
        raise


# ── global hotkey via Win32 RegisterHotKey ──────────────────────────────────
# The `keyboard` low-level hook was silent on this machine (registered fine but
# never fired). RegisterHotKey is the OS-native mechanism and is far more reliable.
_MOD = {"alt": 0x0001, "ctrl": 0x0002, "control": 0x0002,
        "shift": 0x0004, "win": 0x0008, "windows": 0x0008}
_MOD_NOREPEAT = 0x4000
_VK = {"space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B,
       "escape": 0x1B, "backspace": 0x08, "delete": 0x2E, "up": 0x26, "down": 0x28,
       "left": 0x25, "right": 0x27, "home": 0x24, "end": 0x23, "pageup": 0x21,
       "pagedown": 0x22, "insert": 0x2D}
for _i in range(1, 13):
    _VK[f"f{_i}"] = 0x6F + _i

_HOTKEY_ID = 1
_hk_new = None            # pending hotkey string for the loop to (re)register
_hk_lock = threading.Lock()
_hk_applied = threading.Event()
_hk_thread = None
_hk_ok = False
_callbacks = (None, None)


def _parse_hotkey(s):
    """'ctrl+shift+space' → (modifier_flags, virtual_key) ; vk None if no main key."""
    mods, vk = 0, None
    for p in (s or "").lower().replace(" ", "").split("+"):
        if not p:
            continue
        if p in _MOD:
            mods |= _MOD[p]
        elif p in _VK:
            vk = _VK[p]
        elif len(p) == 1:
            vk = ord(p.upper())
    return mods, vk


def hotkey(spec, on_press, on_release):
    """Register `spec` as the global hotkey, replacing the previous one. on_press
    fires on key-down, on_release when the main key comes back up (both on the
    hotkey thread). Returns True if Windows accepted the combo. Raises ValueError,
    leaving the current hotkey untouched, if `spec` has no normal key."""
    global _hk_new, _hk_thread, _callbacks
    if _parse_hotkey(spec)[1] is None:
        raise ValueError(f"'{spec}' has no normal key (modifier-only unsupported)")
    _callbacks = (on_press, on_release)
    _hk_applied.clear()
    with _hk_lock:
        _hk_new = spec
    if _hk_thread is None or not _hk_thread.is_alive():
        _hk_thread = threading.Thread(target=_hotkey_loop, daemon=True)
        _hk_thread.start()
    _hk_applied.wait(1.0)
    return _hk_ok


def _hotkey_loop():
    global _hk_new, _hk_ok
    u = ctypes.windll.user32
    msg = wintypes.MSG()
    u.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # force-create this thread's message queue
    registered = down = False
    vk_main = None

    def key_down(vk):
        return vk and bool(u.GetAsyncKeyState(vk) & 0x8000)

    while True:
        with _hk_lock:
            pending, _hk_new = _hk_new, None
        if pending:
            if registered:
                u.UnregisterHotKey(None, _HOTKEY_ID)
            mods, vk = _parse_hotkey(pending)
            registered = _hk_ok = bool(u.RegisterHotKey(None, _HOTKEY_ID, mods | _MOD_NOREPEAT, vk))
            if registered:
                vk_main = vk
            _hk_applied.set()

        while u.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):  # PM_REMOVE
            if msg.message == 0x0312:  # WM_HOTKEY (key pressed down)
                down = True
                _callbacks[0]()
            u.TranslateMessage(ctypes.byref(msg))
            u.DispatchMessageW(ctypes.byref(msg))

        if down and not key_down(vk_main):
            down = False
            _callbacks[1]()

        time.sleep(0.015)
