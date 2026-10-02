"""
Flow Studio — one desktop app that bundles both tools:

  • Dictation   (Flow — voice → cleaned text, typed into any app)
  • Text to Speech (Kokoro TTS Studio)

It launches both local servers in the background and shows them in a single
native window (Edge WebView2), with tabs to switch between them. No browser,
no terminal — double-click the desktop shortcut.

  python flow_studio.py            → opens the app window
  pythonw flow_studio.py           → same, with no console window (used by the shortcut)
  python flow_studio.py --selftest → starts both servers, runs a Kokoro → Whisper round trip, exits
"""

import difflib
import os
import re
import socket
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ICON_PATH = BASE_DIR / "flow.ico"
ICNS_PATH = BASE_DIR / "flow.icns"   # Dock icon on the Mac
sys.path.insert(0, str(BASE_DIR))

from paths import data_dir

if getattr(sys, "frozen", False):
    # Windowed build: no console, so stdout/stderr are None and any print/tqdm would crash.
    if sys.stdout is None or sys.stderr is None:
        data_dir().mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = open(data_dir() / "flow_studio.log", "w", encoding="utf-8", buffering=1)
    # The installer ships the default models as a Hugging Face cache next to the exe.
    # ponytail: user downloads land there too; #14 decides bundled-vs-user cache.
    os.environ.setdefault("HF_HOME", str(Path(sys.executable).resolve().parent / "models"))

import app as tts   # Kokoro TTS Studio  (Flask app on :7500)
import flow         # Flow dictation      (Flask app on :7600)

TTS_PORT, FLOW_PORT = 7500, 7600
OVERLAY_W, OVERLAY_H = 124, 44   # window; the pill inside shrink-wraps its content (rest transparent)


def _serve(flask_app, port):
    flask_app.run(host="127.0.0.1", port=port, debug=False,
                  use_reloader=False, threaded=True)


def _fatal(msg):
    """Show an error the user can see even under pythonw (no console), then exit."""
    print(msg)
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, msg, "Flow Studio", 0x10)  # MB_ICONERROR
    except Exception:
        pass
    sys.exit(1)


def start_servers():
    flow.start()
    threading.Thread(target=_serve, args=(tts.app, TTS_PORT), daemon=True).start()
    threading.Thread(target=_serve, args=(flow.app, FLOW_PORT), daemon=True).start()


def wait_ready(timeout=25):
    """Block until both servers answer, so the iframes don't load too early."""
    deadline = time.time() + timeout
    for port in (TTS_PORT, FLOW_PORT):
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1).read(1)
                break
            except Exception:
                time.sleep(0.25)
        else:
            return False
    return True


SELFTEST_SENTENCE = "The quick brown fox jumps over the lazy dog."


def _words(text):
    return re.findall(r"[a-z']+", text.lower())


def selftest():
    """Both servers answer, then a round trip on the bundled models: Kokoro speaks a fixed
    sentence, Whisper small transcribes it, the text must fuzzy-match.
    Returns None on success, else the step that broke and why."""
    import numpy as np
    import soundfile as sf
    start_servers()
    if not wait_ready():
        return f"servers: :{TTS_PORT} / :{FLOW_PORT} didn't answer"
    try:
        audio = np.concatenate([np.asarray(r.audio, dtype=np.float32)
                                for r in tts.get_pipeline("a")(SELFTEST_SENTENCE, voice="af_heart")])
    except Exception as exc:
        return f"Kokoro TTS: {exc!r}"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "selftest.wav"
            sf.write(wav, audio, tts.SAMPLE_RATE)  # a file, so faster-whisper resamples 24 → 16 kHz
            heard, _ = flow.transcribe_seg(flow.get_whisper("small"), str(wav), "en")
    except Exception as exc:
        return f"Whisper transcribe: {exc!r}"
    score = difflib.SequenceMatcher(None, _words(SELFTEST_SENTENCE), _words(heard)).ratio()
    print(f"  heard {heard!r} (similarity {score:.2f})")
    if score < 0.8:
        return f"match: expected {SELFTEST_SENTENCE!r}, heard {heard!r}"
    return None


SHELL_HTML = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>
  *{{margin:0;padding:0;box-sizing:border-box;}}
  html,body{{height:100%;}}
  body{{font-family:'Source Sans 3',system-ui,sans-serif;background:#F5F1EB;display:flex;flex-direction:column;overflow:hidden;}}
  .bar{{display:flex;align-items:center;gap:16px;padding:8px 16px;background:#F5F1EB;border-bottom:1px solid #EAE3D6;flex:none;-webkit-app-region:drag;}}
  .logo{{display:flex;align-items:center;gap:9px;}}
  .mark{{width:26px;height:26px;border-radius:8px;background:#1F1D1A;display:flex;align-items:center;justify-content:center;gap:2px;}}
  .mark i{{width:3px;border-radius:2px;background:#F5F1EB;}}
  .mark i.o{{background:#E8912D;}}
  .name{{font-size:15px;font-weight:700;letter-spacing:-.01em;color:#1F1D1A;}}
  .tabs{{display:flex;gap:4px;margin-left:8px;-webkit-app-region:no-drag;}}
  .tab{{font:inherit;font-size:13.5px;font-weight:600;color:#5C564B;background:transparent;border:1px solid transparent;border-radius:8px;padding:6px 14px;cursor:pointer;}}
  .tab:hover{{background:rgba(31,29,26,.05);}}
  .tab.active{{background:#FFFFFF;border-color:#ECE5D8;color:#1F1D1A;box-shadow:0 1px 2px rgba(60,50,30,.05);}}
  .frames{{flex:1;position:relative;min-height:0;}}
  iframe{{position:absolute;inset:0;width:100%;height:100%;border:none;background:#fff;}}
  iframe.hidden{{visibility:hidden;pointer-events:none;}}
</style></head><body>
  <div class="bar">
    <div class="logo"><div class="mark"><i style="height:8px"></i><i class="o" style="height:15px"></i><i style="height:11px"></i></div><span class="name">Flow Studio</span></div>
    <div class="tabs">
      <button class="tab active" data-t="flow">Dictation</button>
      <button class="tab" data-t="tts">Text to Speech</button>
    </div>
  </div>
  <div class="frames">
    <iframe id="flow" src="http://127.0.0.1:{FLOW_PORT}/" allow="clipboard-read; clipboard-write; microphone"></iframe>
    <iframe id="tts" class="hidden" src="http://127.0.0.1:{TTS_PORT}/" allow="clipboard-read; clipboard-write"></iframe>
  </div>
<script>
  const tabs=document.querySelectorAll('.tab');
  tabs.forEach(t=>t.onclick=()=>{{
    tabs.forEach(x=>x.classList.toggle('active',x===t));
    document.getElementById('flow').classList.toggle('hidden',t.dataset.t!=='flow');
    document.getElementById('tts').classList.toggle('hidden',t.dataset.t!=='tts');
  }});
</script>
</body></html>"""


def _hwnd_of(win):
    try:
        h = win.native.Handle
        return int(h.ToInt64()) if hasattr(h, "ToInt64") else int(h)
    except Exception:
        return 0


class _OverlayApi:
    """JS→Python bridge so the overlay can drag its own no-activate window."""
    def __init__(self):
        self.hwnd = 0

    def drag(self, dx, dy):
        if not self.hwnd:
            return
        try:
            import ctypes
            from ctypes import wintypes
            u = ctypes.windll.user32
            rect = wintypes.RECT()
            u.GetWindowRect(ctypes.c_void_p(self.hwnd), ctypes.byref(rect))
            SWP = 0x0010 | 0x0004 | 0x0001  # NOACTIVATE | NOZORDER | NOSIZE
            u.SetWindowPos(ctypes.c_void_p(self.hwnd), None,
                           int(rect.left + dx), int(rect.top + dy), 0, 0, SWP)
        except Exception:
            pass


def _overlay_noactivate(hwnd):
    """Mark the overlay as a no-activate tool window so it never takes focus —
    the user's text field keeps focus and the paste lands there."""
    try:
        import ctypes
        GWL_EXSTYLE, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW = -20, 0x08000000, 0x00000080
        u = ctypes.windll.user32
        ex = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
        u.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
    except Exception:
        pass


def _win_overlay(overlay, api):
    """(show, hide) for the overlay on Windows: a no-activate topmost tool window."""
    import ctypes
    import webview
    try:
        scr = webview.screens[0]
        overlay.move(int((scr.width - OVERLAY_W) / 2), int(scr.height - OVERLAY_H - 20))
    except Exception:
        pass
    hwnd = 0
    for _ in range(20):      # wait for WebView2 to create the native window
        hwnd = _hwnd_of(overlay)
        if hwnd:
            break
        time.sleep(0.15)
    if not hwnd:             # fallback if we couldn't get the handle
        return overlay.show, overlay.hide
    _overlay_noactivate(hwnd)
    api.hwnd = hwnd          # let the JS drag bridge move this window
    u = ctypes.windll.user32
    u.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    u.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    HWND_TOPMOST = ctypes.c_void_p(-1)
    SWP = 0x0010 | 0x0040 | 0x0002 | 0x0001  # NOACTIVATE | SHOWWINDOW | NOMOVE | NOSIZE
    SW_HIDE = 0

    def show():
        u.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP)  # show topmost, no activate

    return show, lambda: u.ShowWindow(hwnd, SW_HIDE)


def _mac_overlay(overlay):
    """(show, hide) for the overlay on the Mac. The overlay's web view moves into a
    borderless non-activating NSPanel: it floats over every app and Space and takes
    clicks (pywebview's easy_drag moves it) without activating Flow Studio, so focus
    stays in the user's text field. pywebview's own window for it is never shown."""
    import AppKit
    from PyObjCTools import AppHelper
    overlay.events.loaded.wait(15)
    built, box = threading.Event(), {}

    def build():  # AppKit wants the main thread
        try:
            scr = AppKit.NSScreen.mainScreen().visibleFrame()
            rect = AppKit.NSMakeRect(scr.origin.x + (scr.size.width - OVERLAY_W) / 2,
                                     scr.origin.y + 20, OVERLAY_W, OVERLAY_H)
            p = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
                rect, AppKit.NSWindowStyleMaskBorderless | AppKit.NSWindowStyleMaskNonactivatingPanel,
                AppKit.NSBackingStoreBuffered, False)
            p.setLevel_(AppKit.NSStatusWindowLevel)
            p.setCollectionBehavior_(AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
                                     | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary)
            p.setHidesOnDeactivate_(False)  # Flow Studio is almost never the active app
            p.setOpaque_(False)
            p.setHasShadow_(False)
            p.setBackgroundColor_(AppKit.NSColor.clearColor())
            p.setContentView_(overlay.native.contentView())
            box["panel"] = p
        finally:
            built.set()

    AppHelper.callAfter(build)
    built.wait(5)
    p = box.get("panel")
    if not p:  # no overlay beats one that steals focus on every show
        print("[!] Couldn't build the dictation overlay panel.")
        return (lambda: None), (lambda: None)
    return (lambda: AppHelper.callAfter(p.orderFrontRegardless),
            lambda: AppHelper.callAfter(p.orderOut_, None))


def _overlay_controller(overlay, api):
    """Show the floating bar while dictating/processing; hide it when idle —
    showing WITHOUT activating it, so focus stays in the user's text field."""
    time.sleep(0.8)
    show, hide = _mac_overlay(overlay) if sys.platform == "darwin" else _win_overlay(overlay, api)
    hide()  # start hidden
    shown = False
    while True:
        active = flow.state.get("status") in ("recording", "transcribing", "cleaning")
        try:
            if active and not shown:
                show(); shown = True
            elif not active and shown:
                hide(); shown = False
        except Exception:
            pass
        time.sleep(0.2)


def _set_window_icon(win):
    """Put flow.ico on the taskbar / Task Manager entry (instead of the generic
    pythonw icon) so the app reads as 'Flow Studio'."""
    try:
        import ctypes
        u = ctypes.windll.user32
        u.GetAncestor.restype = ctypes.c_void_p
        u.LoadImageW.restype = ctypes.c_void_p
        hwnd = 0
        for _ in range(20):          # wait for WebView2 to create the native window
            hwnd = _hwnd_of(win)
            if hwnd:
                break
            time.sleep(0.15)
        if not hwnd:
            return
        root = u.GetAncestor(ctypes.c_void_p(hwnd), 2) or hwnd   # GA_ROOT
        IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x0010, 0x0040
        WM_SETICON, ICON_SMALL, ICON_BIG = 0x0080, 0, 1
        big = u.LoadImageW(None, str(ICON_PATH), IMAGE_ICON, 0, 0,
                           LR_LOADFROMFILE | LR_DEFAULTSIZE)
        small = u.LoadImageW(None, str(ICON_PATH), IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
        u.SendMessageW(ctypes.c_void_p(root), WM_SETICON, ICON_BIG, ctypes.c_void_p(big))
        u.SendMessageW(ctypes.c_void_p(root), WM_SETICON, ICON_SMALL, ctypes.c_void_p(small))
    except Exception:
        pass


def _run_tray(win):
    """System-tray icon (bottom-right). Left-click restores the window; 'Quit'
    fully exits — including the background dictation hotkey."""
    try:
        import pystray
        from PIL import Image
    except Exception:
        return

    def _show(icon=None, item=None):
        win.show()

    def _quit(icon, item):
        icon.stop()
        os._exit(0)   # ponytail: hard exit; daemon servers + hotkey die with the process

    menu = pystray.Menu(
        pystray.MenuItem("Show Flow Studio", _show, default=True),
        pystray.MenuItem("Quit Flow Studio", _quit),
    )
    pystray.Icon("flow_studio", Image.open(ICON_PATH), "Flow Studio", menu).run()


def _mac_dock(main_win):
    """Mac Dock behavior (no tray: pystray and pywebview both need the main thread):
    clicking the Dock icon reopens the hidden window; Cmd+Q, or Quit in the Dock
    menu, exits for real. Call before webview.start(): pywebview builds its app
    delegate from BrowserView.AppDelegate when it makes the first window."""
    import objc
    from webview.platforms.cocoa import BrowserView

    class FlowAppDelegate(BrowserView.AppDelegate):
        def applicationShouldTerminate_(self, app):
            # pywebview would ask each window's `closing` handler, which only hides
            os._exit(0)   # ponytail: hard exit, like the Windows tray's Quit

        @objc.typedSelector(b"Z@:@Z")
        def applicationShouldHandleReopen_hasVisibleWindows_(self, app, visible):
            main_win.show()
            return True

    BrowserView.AppDelegate = FlowAppDelegate


def _startup(main_win, overlay, api):
    """Runs once after the GUI is up: set the icon, make the close button hide the
    window (Flow keeps dictating in the background; quit from the tray or with
    Cmd+Q), then run the overlay loop."""
    mac = sys.platform == "darwin"
    if not mac:
        _set_window_icon(main_win)

    def _hide_to_tray():
        main_win.hide()
        return False   # cancel the close

    main_win.events.closing += _hide_to_tray
    if not mac:
        threading.Thread(target=_run_tray, args=(main_win,), daemon=True).start()
    _overlay_controller(overlay, api)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # Windows console defaults to cp1252
    except Exception:
        pass
    if "--selftest" in sys.argv:
        broken = selftest()
        print("selftest:", f"FAIL at {broken}" if broken else "PASS")
        sys.exit(1 if broken else 0)

    # Single-instance guard: if the servers are already up (app already running),
    # don't try to bind the ports again — just open a window onto them.
    already = False
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{FLOW_PORT}/", timeout=1).read(1)
        already = True
    except Exception:
        pass
    if not already:
        # Werkzeug swallows bind errors inside its server thread, so probe the ports first.
        for _port in (TTS_PORT, FLOW_PORT):
            with socket.socket() as _s:
                if _s.connect_ex(("127.0.0.1", _port)) == 0:
                    _fatal(f"Flow Studio can't start — port {_port} is already in use.\n\n"
                           "Another program is using it. Close it and try again.")
        start_servers()
        if not wait_ready():
            _fatal("Flow Studio can't start — the local servers didn't respond in time.")
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("FlowStudio.App")
    except Exception:
        pass
    try:
        import webview
        main_win = webview.create_window("Flow Studio", html=SHELL_HTML,
                                         width=1200, height=840, min_size=(920, 640))
        api = _OverlayApi()
        overlay = webview.create_window(
            "Flow overlay", url=f"http://127.0.0.1:{FLOW_PORT}/overlay",
            width=OVERLAY_W, height=OVERLAY_H, frameless=True, on_top=True,
            resizable=False, hidden=True, transparent=True, js_api=api)
        if sys.platform == "darwin":
            _mac_dock(main_win)
            webview.start(lambda: _startup(main_win, overlay, api), icon=str(ICNS_PATH))
        else:
            webview.start(lambda: _startup(main_win, overlay, api))
    except Exception as exc:
        # No native window available → fall back to the default browser.
        import webbrowser
        print(f"[!] Native window unavailable ({exc}); opening in browser.")
        webbrowser.open(f"http://127.0.0.1:{FLOW_PORT}/")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
