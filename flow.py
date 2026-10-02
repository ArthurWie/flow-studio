"""
Flow — a local, offline voice-dictation app.

Press Alt+Space anywhere (or click the mic in the dashboard) to dictate. Flow
records from your microphone, transcribes it locally with faster-whisper, cleans
up the filler words with a local Ollama model, and types the result straight into
whatever app you have focused.

  python flow.py            → starts the service + opens the dashboard
  python flow.py --selftest → verifies the ASR + cleanup pipeline (no mic needed)

Everything runs on your machine. Nothing leaves it.
UI is a faithful build of the "Flow" design mockup (cream / ink / orange).
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import numpy as np

from paths import data_dir
if sys.platform == "darwin":
    import os_mac as osi
else:
    import os_win as osi  # ponytail: os_linux joins here in phase 4

BASE_DIR = Path(__file__).resolve().parent
# Writable data lives outside the (possibly read-only) install dir.
DATA_DIR = data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)
HIST_FILE = DATA_DIR / "flow_history.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
REC_SR = 16000  # faster-whisper wants 16 kHz mono
OLLAMA_URL = "http://127.0.0.1:11434"  # NOT localhost: on Windows it tries IPv6 first and adds ~2s/call
DEBUG_LOG = DATA_DIR / "flow_debug.log"


def _dbg(msg):
    try:
        with open(DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass

# faster-whisper size for each ASR row shown in the model panel.
WHISPER_SIZES = {
    "faster-whisper-small": "small",
    "faster-whisper-medium": "medium",
    "faster-whisper-large-v3": "large-v3",
}

settings = {
    "whisper_row": "faster-whisper-small",  # which model row is selected
    "cleanup_model": "qwen2.5:3b",          # ollama model for cleanup (better German than llama3.2)
    # alt+space is a Windows system shortcut; ctrl+alt=AltGr on DE keyboards
    "hotkey": "cmd+shift+space" if sys.platform == "darwin" else "ctrl+shift+space",
    "language": None,                       # None = auto-detect; or "en", "de", …
    "mic_index": None,                      # None = system default input device
    "overlay_style": "equalizer",           # equalizer | line | dots | orb
}


def load_settings():
    """Overlay settings.json onto the defaults; unknown keys and a corrupt file are ignored."""
    try:
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        settings.update({k: v for k, v in saved.items() if k in settings})
    except (OSError, ValueError, AttributeError):
        pass


def save_settings():
    try:
        SETTINGS_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    except OSError as exc:
        _dbg(f"save settings: {exc}")


# Shared state the dashboard polls. status: idle|recording|transcribing|cleaning|done
state = {
    "status": "idle",
    "raw": "",
    "clean": "",
    "app": "",
    "error": "",
    "started": 0.0,
    "ollama_ok": None,
    "ollama_setup": None,   # None | "running" | "done" | "error: …"
    "hotkey_ok": None,
}

_state_lock = threading.Lock()


# ── microphone capture ──────────────────────────────────────────────────────
import queue  # noqa: E402

_stream = None
_audio_q = queue.Queue()          # mic frames flow here; a worker drains them live
_cur_level = 0.0                  # live mic loudness 0..1, drives the overlay waveform


def start_capture():
    global _stream
    import sounddevice as sd

    def cb(indata, n, t, status):
        global _cur_level
        _audio_q.put(indata.copy())
        try:
            _cur_level = min(1.0, float(np.sqrt(np.mean(indata ** 2))) * 14.0)
        except Exception:
            pass

    def open_stream(device):
        s = sd.InputStream(samplerate=REC_SR, channels=1, dtype="float32",
                           callback=cb, device=device)
        try:
            s.start()
        except Exception:
            s.close()
            raise
        return s

    dev = settings.get("mic_index")
    try:
        _stream = open_stream(dev)
    except Exception as exc:
        if dev is None:
            raise
        # device indices shift on plug/unplug/reboot; a stale saved index shouldn't kill dictation
        _dbg(f"mic {dev} failed ({exc}); using default input")
        _stream = open_stream(None)


def stop_capture():
    global _stream, _cur_level
    _cur_level = 0.0
    if _stream is not None:
        try:
            _stream.stop()
            _stream.close()
        except Exception:
            pass
        _stream = None


# ── transcription (faster-whisper) ──────────────────────────────────────────
_wmodel = None
_wsize = None


def get_whisper(size):
    global _wmodel, _wsize
    if _wmodel is None or size != _wsize:
        from faster_whisper import WhisperModel
        _wmodel = WhisperModel(whisper_path(size), device="cpu", compute_type="int8")
        _wsize = size
    return _wmodel


@contextmanager
def hf_online():
    """Lift HF_HUB_OFFLINE for one user-started download (huggingface_hub reads it per request).
    ponytail: process-wide flag, so a model load racing the download may check revisions too."""
    from huggingface_hub import constants
    was, constants.HF_HUB_OFFLINE = constants.HF_HUB_OFFLINE, False
    try:
        yield
    finally:
        constants.HF_HUB_OFFLINE = was


def whisper_path(size):
    """The model folder from the bundled cache (HF_HUB_CACHE) or the user cache (HF_HOME/hub),
    with no network. Missing from both: download it into the user cache, since picking
    a size in the model panel is a user-started download."""
    from faster_whisper.utils import download_model
    from huggingface_hub import constants
    from huggingface_hub.errors import LocalEntryNotFoundError
    user_cache = str(Path(constants.HF_HOME) / "hub")
    for cache in (constants.HF_HUB_CACHE, user_cache):
        try:
            return download_model(size, local_files_only=True, cache_dir=cache)
        except LocalEntryNotFoundError:
            pass
    with hf_online():
        return download_model(size, cache_dir=user_cache)


def transcribe_seg(model, audio, lang):
    """Transcribe one audio segment. Returns (text, detected_lang)."""
    segments, info = model.transcribe(audio, language=lang, beam_size=5)
    text = " ".join(s.text.strip() for s in segments).strip()
    return text, getattr(info, "language", None)


# ── cleanup (local Ollama LLM) ──────────────────────────────────────────────
# A prompt in the transcript's own language keeps a weaker model from drifting or
# translating. Both are strict: remove fillers + fix punctuation, change nothing else.
CLEANUP_PROMPT_EN = (
    "You clean up a dictated English transcript. Make ONLY these edits: delete filler words "
    "(uh, um, like, you know, I mean), delete stutters and immediately repeated words, and add "
    "correct punctuation and capitalization. Do NOT rephrase, reorder, summarize, translate, or "
    "change any wording. Keep the meaning exactly, especially negations (not, no, never). "
    "Output only the cleaned text, nothing else.\n\nTranscript:\n"
)
CLEANUP_PROMPT_DE = (
    "Du bereinigst einen diktierten deutschen Text. Nimm NUR diese Änderungen vor: entferne "
    "Füllwörter (äh, ähm, halt, quasi, sozusagen, ne), entferne Stotterer und direkt doppelte "
    "Wörter, setze korrekte Satzzeichen und Groß-/Kleinschreibung. Formuliere NICHTS um, ändere "
    "keine Wörter, kürze nichts, übersetze nicht. Behalte Reihenfolge und Sinn exakt bei — "
    "besonders Verneinungen (nicht, kein, nie). Antworte auf Deutsch. Gib nur den bereinigten "
    "Text aus, sonst nichts.\n\nTranskript:\n"
)


def ollama_models():
    """Return [(name, size_bytes), ...] or None if Ollama is unreachable."""
    try:
        data = json.loads(urllib.request.urlopen(OLLAMA_URL + "/api/tags", timeout=2).read())
        return [(m["name"], m.get("size", 0)) for m in data.get("models", [])]
    except Exception:
        return None


def ollama_up():
    return ollama_models() is not None


OLLAMA_INSTALLER_URL = "https://ollama.com/download/OllamaSetup.exe"


def ollama_exe():
    found = shutil.which("ollama")
    if found:
        return found
    cand = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
    return str(cand) if cand.exists() else "ollama"


def setup_ollama():
    """Optional cleanup setup, started from the dashboard: install Ollama if it's missing
    (only a validly signed installer is run), then pull the cleanup model.
    Progress lands in state["ollama_setup"]; failure is non-fatal (raw transcripts)."""
    state["ollama_setup"] = "running"
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        if not ollama_up() and not shutil.which("ollama"):
            if sys.platform != "win32":
                raise RuntimeError("automatic Ollama install is Windows-only for now; install it from ollama.com")
            dest = DATA_DIR / "OllamaSetup.exe"
            with urllib.request.urlopen(OLLAMA_INSTALLER_URL, timeout=60) as r, open(dest, "wb") as f:
                shutil.copyfileobj(r, f)
            if not osi.signature_ok(dest):
                dest.unlink(missing_ok=True)
                raise RuntimeError("the downloaded Ollama installer failed signature verification; not running it")
            subprocess.run([str(dest), "/SILENT"], check=True)  # its own installer UI may show
        subprocess.run([ollama_exe(), "pull", "qwen2.5:3b"], check=True, creationflags=no_window)
        state["ollama_ok"] = ollama_up()
        resolve_cleanup_model()
        state["ollama_setup"] = "done"
    except Exception as exc:
        state["ollama_setup"] = f"error: {exc}"


def resolve_cleanup_model():
    """Keep the preferred model if installed; otherwise pick the best available.
    Prefer instruction-tuned models over deepseek-r1 (reasoning models ramble),
    and smaller over larger (faster, and a 3B is plenty for cleanup)."""
    models = ollama_models()
    if not models:
        return  # Ollama down; leave the preferred name so the UI shows intent
    names = [n for n, _ in models]
    if settings["cleanup_model"] in names:
        return
    pref = ("qwen2.5", "qwen2", "qwen", "llama", "mistral", "gemma", "phi")
    def rank(item):
        name, size = item
        p = next((i for i, x in enumerate(pref) if name.startswith(x)), 90)
        if name.startswith("deepseek-r1"):
            p = 99  # reasoning models are poor at "return only the cleaned text"
        return (p, size)
    settings["cleanup_model"] = sorted(models, key=rank)[0][0]
    print(f"  [i] qwen2.5:3b not installed - using '{settings['cleanup_model']}' for cleanup.")
    print("      For best results: ollama pull qwen2.5:3b")


def cleanup(raw):
    raw = (raw or "").strip()
    if not raw:
        return ""
    lang = (state.get("detected_lang") or settings.get("language") or "").lower()
    prompt = CLEANUP_PROMPT_DE if lang.startswith("de") else CLEANUP_PROMPT_EN
    body = json.dumps({
        "model": settings["cleanup_model"],
        "prompt": prompt + raw,
        "stream": False,
        "options": {"temperature": 0.0},
    }).encode()
    try:
        req = urllib.request.Request(OLLAMA_URL + "/api/generate", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            out = json.loads(r.read()).get("response", "")
    except Exception:
        return raw  # Ollama down / model missing → fall back to the raw transcript
    out = re.sub(r"<think>.*?</think>", "", out, flags=re.S)  # strip reasoning-model noise
    out = out.strip().strip('"').strip()
    return out or raw


# ── typing into the focused app ─────────────────────────────────────────────
def type_text(text):
    try:
        osi.paste(text, state.get("target_hwnd") or 0)
    except PermissionError:
        raise  # the adapter's message tells the user how to fix it
    except Exception as exc:
        _dbg(f"type_text error: {exc}")


# ── history ─────────────────────────────────────────────────────────────────
def load_history():
    if HIST_FILE.exists():
        try:
            return json.loads(HIST_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def add_history(text, app):
    h = load_history()
    stamp = datetime.now()
    h.insert(0, {
        "time": stamp.strftime("%I:%M %p").lstrip("0"),
        "day": stamp.strftime("%Y-%m-%d"),
        "text": text,
        "app": app or "app",
    })
    HIST_FILE.write_text(json.dumps(h[:200], indent=2, ensure_ascii=False), encoding="utf-8")


# ── the pipeline (streaming) ────────────────────────────────────────────────
# While you talk, a worker transcribes each completed speech segment (cut at
# silences) in the background, so on stop only the final tail remains → the text
# lands much faster on long clips. Partials are never shown; the UI stays on
# "Listening…". Cut points are silences, so words aren't split mid-token.
SIL_THRESH = 0.012   # ponytail: fixed-energy VAD; swap for webrtcvad if noisy rooms misfire
SIL_HANG = 0.7       # seconds of silence that closes a segment
MIN_SEG = 0.4        # ignore segments shorter than this (avoids silence hallucinations)
MAX_SEG = 18.0       # force-close a segment that never pauses (bounds tail latency)
SILENCE_FLOOR = 0.004  # below this RMS the whole clip is treated as true silence

_stop_flag = threading.Event()
_cancel_flag = threading.Event()
_worker = None


def _rms(a):
    return float(np.sqrt(np.mean(a.astype(np.float32) ** 2))) if a.size else 0.0


def _drain_queue():
    while not _audio_q.empty():
        try:
            _audio_q.get_nowait()
        except queue.Empty:
            break


def _stream_worker():
    full_parts, seg_texts = [], []   # full_parts = every frame (safety-net buffer)
    try:
        model = get_whisper(WHISPER_SIZES.get(settings["whisper_row"], "small"))
        lang = settings.get("language") or None  # lock to first detected language once known
        buf_parts, buf_len = [], 0
        have_speech, silence_s, first = False, 0.0, True

        def flush():
            nonlocal buf_parts, buf_len, have_speech, silence_s, lang, first
            if buf_len / REC_SR >= MIN_SEG:
                txt, lg = transcribe_seg(model, np.concatenate(buf_parts), lang)
                if lang is None and lg:
                    lang = lg
                if first and lg:
                    state["detected_lang"] = lg
                    first = False
                if txt:
                    seg_texts.append(txt)
            buf_parts, buf_len = [], 0
            have_speech, silence_s = False, 0.0

        while not _stop_flag.is_set() and not _cancel_flag.is_set():
            try:
                chunk = _audio_q.get(timeout=0.1).flatten()
            except queue.Empty:
                continue
            full_parts.append(chunk)
            buf_parts.append(chunk)
            buf_len += len(chunk)
            if _rms(chunk) >= SIL_THRESH:
                have_speech, silence_s = True, 0.0
            else:
                silence_s += len(chunk) / REC_SR
            if have_speech and (buf_len / REC_SR >= MAX_SEG or silence_s >= SIL_HANG):
                flush()

        # drain whatever the mic pushed after the stop signal
        while True:
            try:
                c = _audio_q.get_nowait().flatten()
                full_parts.append(c)
                buf_parts.append(c)
                buf_len += len(c)
            except queue.Empty:
                break

        if _cancel_flag.is_set():
            with _state_lock:
                state.update(status="idle", raw="", clean="")
            return

        with _state_lock:
            state["status"] = "transcribing"
        flush()  # final tail — MIN_SEG gate only, NO energy gate (never drop real audio)
        raw = " ".join(seg_texts).strip()
        # Safety net: if segmentation produced nothing but we captured audio,
        # transcribe the whole recording — matches the original batch behaviour.
        if not raw and full_parts:
            full = np.concatenate(full_parts)
            if _rms(full) >= SILENCE_FLOOR:
                txt, lg = transcribe_seg(model, full, lang)
                if lg and not state.get("detected_lang"):
                    state["detected_lang"] = lg
                raw = txt.strip()
        _finalize(raw)
    except Exception as exc:
        # Never leave the UI stuck on "transcribing".
        with _state_lock:
            state.update(status="idle", error="Dictation failed: " + str(exc))


def _finalize(raw):
    try:
        if not raw:
            with _state_lock:
                state.update(status="idle", error="No speech detected — try again.")
            return
        with _state_lock:
            state.update(status="cleaning", raw=raw)
        clean = cleanup(raw)
        app = state["app"]
        try:
            type_text(clean)
        except PermissionError as exc:  # e.g. no Accessibility on macOS; text is on the clipboard
            add_history(clean, app)
            with _state_lock:
                state.update(status="idle", clean=clean, error=str(exc))
            return
        add_history(clean, app)
        with _state_lock:
            state.update(status="done", clean=clean)
    except Exception as exc:
        with _state_lock:
            state.update(status="idle", error=str(exc))


def do_start():
    global _worker
    hwnd, app_name = osi.foreground()
    with _state_lock:
        if state["status"] not in ("idle", "done"):  # "done" → start a fresh dictation
            return
        state.update(status="recording", raw="", clean="", error="",
                     detected_lang="", app=app_name, target_hwnd=hwnd, started=time.time())
    _stop_flag.clear()
    _cancel_flag.clear()
    _drain_queue()
    start_capture()
    _worker = threading.Thread(target=_stream_worker, daemon=True)
    _worker.start()


def do_stop():
    with _state_lock:
        if state["status"] != "recording":
            return
        state["status"] = "transcribing"
    _stop_flag.set()      # worker finishes the tail, then cleans up + types
    stop_capture()


def do_cancel():
    with _state_lock:
        if state["status"] not in ("recording", "transcribing", "cleaning"):
            return
    _cancel_flag.set()
    _stop_flag.set()
    stop_capture()
    with _state_lock:
        state.update(status="idle", raw="", clean="", error="")


# ── global hotkey gestures (the OS adapter delivers press / release) ────────
# Gesture state machine (Wispr-Flow style):
#   hold ≥ HOLD_MIN then release  → push-to-talk, auto-confirm on release
#   quick tap (single or double)  → hands-free; recording continues until the
#                                   next hotkey press or the ✓ in the overlay
HOLD_MIN, DOUBLE_WINDOW = 0.3, 0.4
_gesture = {"mode": "idle", "press_start": 0.0, "tap1": 0.0}  # mode: idle | pressing | tap_wait | handsfree


def _gesture_sync(now):
    g = _gesture
    # recording ended by other means (✓/✕ in overlay, cancel) → reset gesture
    if g["mode"] != "idle" and state["status"] != "recording":
        g["mode"] = "idle"
    elif g["mode"] == "tap_wait" and now - g["tap1"] > DOUBLE_WINDOW:
        g["mode"] = "handsfree"                  # single tap → hands-free too
        _dbg("single tap -> hands-free")


def _on_hotkey_press():
    now = time.time()
    _gesture_sync(now)
    g = _gesture
    if g["mode"] == "idle":
        do_start()
        if state["status"] == "recording":
            g["mode"], g["press_start"] = "pressing", now
            _dbg("hotkey down -> recording")
    elif g["mode"] == "tap_wait":
        g["mode"] = "handsfree"                  # second quick tap
        _dbg("double-tap -> hands-free")
    elif g["mode"] == "handsfree":
        do_stop(); g["mode"] = "idle"            # press again ends hands-free
        _dbg("hands-free press -> stop")


def _on_hotkey_release():
    now = time.time()
    _gesture_sync(now)
    g = _gesture
    if g["mode"] == "pressing":
        held = now - g["press_start"]
        if held >= HOLD_MIN:
            do_stop(); g["mode"] = "idle"        # held then released → confirm
            _dbg(f"release after {held:.2f}s -> confirm")
        else:
            g["mode"], g["tap1"] = "tap_wait", now   # quick tap → wait for a 2nd


def set_hotkey(spec):
    """(Re)register the global hotkey. Raises ValueError for a modifier-only combo."""
    ok = osi.hotkey(spec, _on_hotkey_press, _on_hotkey_release)
    state["hotkey_ok"] = ok
    _dbg(f"hotkey('{spec}') -> {ok}")
    return ok


def start():
    """Init shared by `python flow.py` and flow_studio: Ollama probe, cleanup model,
    global hotkey. Returns whether the hotkey is active."""
    load_settings()
    state["ollama_ok"] = ollama_up()
    resolve_cleanup_model()
    try:
        return set_hotkey(settings["hotkey"])
    except ValueError as exc:
        state["hotkey_ok"] = False
        _dbg(f"hotkey: {exc}")
        return False


# ── web app ─────────────────────────────────────────────────────────────────
from flask import Flask, jsonify, request  # noqa: E402

app = Flask(__name__)


@app.route("/")
def index():
    return PAGE_HTML


@app.route("/overlay")
def overlay():
    return OVERLAY_HTML


@app.route("/api/state")
def api_state():
    h = load_history()
    words = sum(len(x["text"].split()) for x in h)
    with _state_lock:
        s = dict(state)
    s.update(history=h, total_words=words, whisper_row=settings["whisper_row"],
             cleanup_model=settings["cleanup_model"], hotkey=settings["hotkey"],
             language=settings["language"] or "auto",
             detected_lang=state.get("detected_lang", ""),
             overlay_style=settings["overlay_style"])
    return jsonify(s)


@app.route("/api/level")
def api_level():
    return jsonify(level=round(_cur_level, 3), status=state["status"],
                   style=settings["overlay_style"])


@app.route("/api/overlay_style", methods=["POST"])
def api_overlay_style():
    st = (request.get_json(force=True) or {}).get("style")
    if st in ("equalizer", "line", "dots", "orb"):
        settings["overlay_style"] = st
        save_settings()
    return jsonify(ok=True, overlay_style=settings["overlay_style"])


@app.route("/api/start", methods=["POST"])
def api_start():
    do_start()
    return jsonify(ok=True)


@app.route("/api/stop", methods=["POST"])
def api_stop():
    do_stop()
    return jsonify(ok=True)


@app.route("/api/cancel", methods=["POST"])
def api_cancel():
    do_cancel()
    return jsonify(ok=True)


@app.route("/api/reset", methods=["POST"])
def api_reset():
    with _state_lock:
        if state["status"] == "done":
            state.update(status="idle", raw="", clean="")
    return jsonify(ok=True)


@app.route("/api/model", methods=["POST"])
def api_model():
    row = (request.get_json(force=True) or {}).get("row")
    if row in WHISPER_SIZES:
        settings["whisper_row"] = row
        save_settings()
    return jsonify(ok=True, whisper_row=settings["whisper_row"])


@app.route("/api/language", methods=["POST"])
def api_language():
    data = request.get_json(silent=True) or {}
    if "value" in data:  # explicit set from the settings dropdown
        v = data["value"]
        settings["language"] = None if v in (None, "", "auto") else v
    else:  # no body → cycle (used by the quick pill in the hero)
        settings["language"] = {None: "en", "en": "de", "de": None}.get(settings["language"])
    save_settings()
    state["detected_lang"] = ""  # forced value should win in the UI
    return jsonify(ok=True, language=settings["language"] or "auto")


@app.route("/api/mics")
def api_mics():
    try:
        import sounddevice as sd
        seen, mics = set(), []
        for i, d in enumerate(sd.query_devices()):
            name = d["name"]
            if d["max_input_channels"] > 0 and name not in seen:
                seen.add(name)
                mics.append({"index": i, "name": name})
    except Exception as e:
        return jsonify(mics=[], current=settings["mic_index"], error=str(e))
    return jsonify(mics=mics, current=settings["mic_index"])


@app.route("/api/mic", methods=["POST"])
def api_mic():
    idx = (request.get_json(force=True) or {}).get("index")
    settings["mic_index"] = None if idx in (None, "", "auto") else int(idx)
    save_settings()
    return jsonify(ok=True, mic_index=settings["mic_index"])


@app.route("/api/ollama_models")
def api_ollama_models():
    return jsonify(models=[n for n, _ in (ollama_models() or [])],
                   current=settings["cleanup_model"])


@app.route("/api/ollama_setup", methods=["POST"])
def api_ollama_setup():
    if state["ollama_setup"] != "running":
        state["ollama_setup"] = "running"   # before the thread starts, so a double click can't race
        threading.Thread(target=setup_ollama, daemon=True).start()
    return jsonify(ok=True)


@app.route("/api/cleanup_model", methods=["POST"])
def api_cleanup_model():
    name = (request.get_json(force=True) or {}).get("model")
    if name:
        settings["cleanup_model"] = name
        save_settings()
    return jsonify(ok=True, cleanup_model=settings["cleanup_model"])


@app.route("/api/hotkey", methods=["POST"])
def api_hotkey():
    hk = ((request.get_json(force=True) or {}).get("hotkey") or "").strip().lower()
    if not hk:
        return jsonify(ok=True, hotkey=settings["hotkey"])
    try:
        ok = set_hotkey(hk)
    except ValueError:
        return jsonify(ok=False, error="Pick a combination that includes a normal key (not only modifiers)."), 400
    settings["hotkey"] = hk
    save_settings()
    return jsonify(ok=ok, hotkey=settings["hotkey"])


OVERLAY_HTML = r"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
  html,body{margin:0;height:100%;background:transparent;overflow:hidden;font-family:system-ui,'Segoe UI',sans-serif;user-select:none;}
  body{display:flex;align-items:center;justify-content:center;}
  #bar{display:inline-flex;align-items:center;gap:6px;background:#17150F;border-radius:16px;padding:4px 6px;box-shadow:0 4px 14px rgba(0,0,0,.4);cursor:move;}
  button{border:none;border-radius:50%;width:24px;height:24px;cursor:pointer;display:flex;align-items:center;justify-content:center;padding:0;font-size:11px;flex:none;}
  #cancel{background:rgba(255,255,255,.14);color:#EDEAE2;}
  #confirm{background:#E8912D;color:#17150F;font-weight:700;}
  #viz{height:18px;display:flex;align-items:center;justify-content:center;gap:2px;flex:none;}
  #viz i{display:block;width:2px;border-radius:1px;background:#F5F1EB;height:4px;}
  #viz i.o{background:#E8912D;}
  .orb{width:11px;height:11px;border-radius:50%;background:#E8912D;}
  #spin{width:14px;height:14px;border:2px solid rgba(255,255,255,.25);border-top-color:#E8912D;border-radius:50%;animation:sp .7s linear infinite;display:none;}
  @keyframes sp{to{transform:rotate(360deg)}}
</style></head><body>
<div id="bar">
  <button id="cancel" title="Cancel">&#10005;</button>
  <div id="viz"></div>
  <div id="spin"></div>
  <button id="confirm" title="Insert">&#10003;</button>
</div>
<script>
  const $=i=>document.getElementById(i), post=u=>fetch(u,{method:'POST'});
  $('cancel').onclick=()=>post('/api/cancel');
  $('confirm').onclick=()=>post('/api/stop');
  // drag to move the window (app-region drag doesn't work on a no-activate window)
  const bar=$('bar'); let dragging=false, lx=0, ly=0;
  bar.addEventListener('pointerdown', e=>{
    if(e.target.closest('button')) return;
    dragging=true; lx=e.screenX; ly=e.screenY;
    try{bar.setPointerCapture(e.pointerId);}catch(_){}
  });
  bar.addEventListener('pointermove', e=>{
    if(!dragging) return;
    const dx=e.screenX-lx, dy=e.screenY-ly; lx=e.screenX; ly=e.screenY;
    if((dx||dy) && window.pywebview && window.pywebview.api && window.pywebview.api.drag) window.pywebview.api.drag(dx, dy);
  });
  bar.addEventListener('pointerup', e=>{ dragging=false; try{bar.releasePointerCapture(e.pointerId);}catch(_){} });
  let curStyle=null;
  function build(style){
    const v=$('viz'); v.innerHTML=''; curStyle=style;
    if(style==='equalizer'){for(let i=0;i<9;i++){const b=document.createElement('i');if(i%4===2)b.className='o';v.appendChild(b);}}
    else if(style==='dots'){for(let i=0;i<5;i++){const d=document.createElement('i');d.style.width='6px';d.style.height='6px';d.style.borderRadius='50%';if(i===2)d.className='o';v.appendChild(d);}}
    else if(style==='orb'){v.innerHTML='<span class="orb"></span>';}
    else if(style==='line'){v.innerHTML='<svg width="46" height="18" viewBox="0 0 46 18"><polyline fill="none" stroke="#F5F1EB" stroke-width="2" stroke-linecap="round" points=""></polyline></svg>';}
  }
  let latest={level:0,status:'idle',style:'equalizer'}, t=0, sm=0;
  setInterval(async()=>{ try{latest=await(await fetch('/api/level')).json();}catch(e){} }, 70);
  function loop(){
    if(latest.style && latest.style!==curStyle) build(latest.style);
    const busy=latest.status==='transcribing'||latest.status==='cleaning';
    $('viz').style.display=busy?'none':'flex';
    $('confirm').style.display=busy?'none':'flex';
    $('spin').style.display=busy?'block':'none';
    if(!busy){
      t+=0.13; sm+=((latest.level||0)-sm)*0.4;
      const v=$('viz');
      if(curStyle==='equalizer'){[...v.children].forEach((b,i)=>{const s=0.35+0.65*Math.abs(Math.sin(t+i*0.7));b.style.height=Math.round(3+sm*15*s)+'px';});}
      else if(curStyle==='dots'){[...v.children].forEach((d,i)=>{const s=0.5+sm*Math.abs(Math.sin(t+i*0.9));d.style.transform='scale('+(0.6+s*0.9).toFixed(2)+')';});}
      else if(curStyle==='orb'){const o=v.querySelector('.orb');if(o)o.style.transform='scale('+(1+sm*0.9).toFixed(2)+')';}
      else if(curStyle==='line'){const pl=v.querySelector('polyline');if(pl){let p='';for(let i=0;i<=11;i++){p+=(i*4.2).toFixed(0)+','+(9+Math.sin(t*1.3+i*0.8)*6*sm).toFixed(1)+' ';}pl.setAttribute('points',p.trim());}}
    }
    requestAnimationFrame(loop);
  }
  build('equalizer'); requestAnimationFrame(loop);
</script></body></html>"""


PAGE_HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Flow — Dictation</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@600&family=Source+Sans+3:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  *{margin:0;padding:0;box-sizing:border-box;}
  body{background:#F5F1EB;color:#1F1D1A;font-family:'Source Sans 3',system-ui,sans-serif;}
  .mono{font-family:'IBM Plex Mono',monospace;}
  .serif{font-family:'Playfair Display',serif;}
  button{font-family:inherit;}
  @keyframes eqbar{0%,100%{height:5px}50%{height:22px}}
  @keyframes caretblink{0%,49%{opacity:1}50%,100%{opacity:0}}
  @keyframes micpulse{0%{box-shadow:0 0 0 0 rgba(232,145,45,.35)}70%{box-shadow:0 0 0 18px rgba(232,145,45,0)}100%{box-shadow:0 0 0 0 rgba(232,145,45,0)}}
  @keyframes pillin{from{opacity:0;transform:translate(-50%,14px)}to{opacity:1;transform:translate(-50%,0)}}
  @keyframes panelin{from{transform:translateX(30px);opacity:0}to{transform:translateX(0);opacity:1}}
  @keyframes spin{to{transform:rotate(360deg)}}
  .navitem:hover{background:rgba(31,29,26,.045);color:#1F1D1A;}
  .row:hover{background:#FBF8F3;}
  .subnav{display:flex;align-items:center;gap:9px;padding:8px 10px;border-radius:8px;font-size:13.5px;font-weight:500;color:#5C564B;cursor:pointer;}
  .subnav:hover{background:rgba(31,29,26,.05);}
  .subnav.active{background:#FFFFFF;border:1px solid #ECE5D8;color:#1F1D1A;font-weight:600;box-shadow:0 1px 2px rgba(60,50,30,.04);}
  select.sel{font-family:'Source Sans 3',sans-serif;font-size:13px;padding:7px 10px;border:1px solid #E2DACB;border-radius:8px;background:#FFFFFF;color:#1F1D1A;cursor:pointer;max-width:200px;}
  select.sel:disabled{color:#A69D89;cursor:default;background:#FBF8F3;}
  .btnchg{font-family:'Source Sans 3',sans-serif;font-size:13px;font-weight:600;color:#1F1D1A;background:#FFFFFF;border:1px solid #E2DACB;border-radius:8px;padding:7px 14px;cursor:pointer;}
  .btnchg:hover{background:#FBF8F3;border-color:#D8CFBC;}
  .setrow:first-child{border-top:none !important;}
  .kbd{font-family:'IBM Plex Mono',monospace;font-size:11.5px;background:#F5F1EB;border:1px solid #E5DDCC;border-radius:5px;padding:2px 7px;}
  .copybtn{display:inline-flex;align-items:center;justify-content:center;width:26px;height:24px;border:1px solid #ECE5D8;background:#FFFFFF;color:#8A8378;border-radius:7px;cursor:pointer;padding:0;flex-shrink:0;}
  .copybtn:hover{color:#1F1D1A;border-color:#D8CFBC;background:#FBF8F3;}
  .copybtn.copied{color:#2E7D43;border-color:#D4E8D9;background:#EAF4EC;}
  .row .copybtn{opacity:0;transition:opacity .12s;}
  .row:hover .copybtn{opacity:1;}
</style></head>
<body>
<div style="min-height:100vh; padding-bottom:60px;">

  <!-- Top bar -->
  <div style="display:flex; align-items:center; justify-content:space-between; padding:16px 40px 0; max-width:1060px; margin:0 auto;">
    <div style="display:flex; align-items:center; gap:10px;">
      <div style="width:30px;height:30px;border-radius:9px;background:#1F1D1A;display:flex;align-items:center;justify-content:center;gap:2.5px;">
        <div style="width:3px;height:8px;border-radius:2px;background:#F5F1EB;"></div>
        <div style="width:3px;height:15px;border-radius:2px;background:#E8912D;"></div>
        <div style="width:3px;height:11px;border-radius:2px;background:#F5F1EB;"></div>
      </div>
      <span class="serif" style="font-size:20px; font-weight:600; letter-spacing:-0.01em;">Flow</span>
      <span class="mono" style="font-size:10.5px; font-weight:500; letter-spacing:.04em; color:#8A8378; border:1px solid #E2DACB; border-radius:6px; padding:2px 7px; background:#FBF8F3;">Basic</span>
    </div>
    <div style="display:flex; align-items:center; gap:16px;">
      <div class="mono" id="todayDate" style="font-size:12px; color:#8A8378;"></div>
      <button id="settingsBtn" title="Settings" class="navitem" style="display:flex; align-items:center; gap:7px; font-size:13px; font-weight:500; color:#5C564B; background:#FFFFFF; border:1px solid #ECE5D8; border-radius:9px; padding:7px 12px; cursor:pointer;">
        <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="8" cy="8" r="2.2"></circle><path d="M8 1.8v2M8 12.2v2M1.8 8h2M12.2 8h2M3.7 3.7l1.4 1.4M10.9 10.9l1.4 1.4M12.3 3.7l-1.4 1.4M5.1 10.9l-1.4 1.4"></path></svg>
        Settings
      </button>
    </div>
  </div>

  <!-- Main -->
  <div style="padding:26px 40px 0;">
    <div style="max-width:1060px; margin:0 auto;">
      <div style="margin-bottom:22px;">
        <h1 class="serif" style="font-size:33px; font-weight:600; letter-spacing:-0.015em; margin:0 0 5px;">Welcome back, Arthur</h1>
        <div style="font-size:14.5px; color:#8A8378;">Everything runs on this PC — nothing leaves your machine.</div>
      </div>

      <div style="display:grid; grid-template-columns:1fr 264px; gap:20px; align-items:start;">
        <div style="display:flex; flex-direction:column; gap:20px; min-width:0;">

          <!-- Hero card -->
          <div style="background:#FFFFFF; border:1px solid #ECE5D8; border-radius:14px; box-shadow:0 1px 3px rgba(60,50,30,.05); padding:20px 24px 26px; position:relative; min-height:308px; display:flex; flex-direction:column;">
            <div style="display:flex; align-items:center; justify-content:space-between; gap:12px;">
              <button id="modelBtn" style="display:flex; align-items:center; gap:8px; font-size:12px; font-weight:500; color:#1F1D1A; background:#FBF8F3; border:1px solid #E9E2D4; border-radius:8px; padding:6px 11px; cursor:pointer; white-space:nowrap; flex-shrink:0;" class="mono">
                <span id="modelDot" style="width:7px; height:7px; border-radius:50%; background:#4CA366; flex-shrink:0;"></span>
                <span id="modelName">faster-whisper-small</span>
                <svg width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="#8A8378" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="m2.5 4 2.5 2.5L7.5 4"></path></svg>
              </button>
              <div class="mono" id="engineLine" style="font-size:11.5px; color:#8A8378; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; min-width:0;">local · en-US · cleanup: qwen2.5:3b</div>
            </div>

            <!-- idle -->
            <div id="idleView" style="flex:1; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:16px; padding:26px 0 6px;">
              <button id="micBtn" title="Start dictating" style="width:76px; height:76px; border-radius:50%; background:#E8912D; border:none; cursor:pointer; display:flex; align-items:center; justify-content:center; box-shadow:0 2px 8px rgba(232,145,45,.35); animation:micpulse 2.6s ease-out infinite;">
                <svg width="28" height="28" viewBox="0 0 16 16" fill="none" stroke="#FFFFFF" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="1.5" width="4" height="8" rx="2"></rect><path d="M3.5 7.5a4.5 4.5 0 0 0 9 0M8 12v2.5"></path></svg>
              </button>
              <div style="text-align:center;">
                <div style="font-size:15px; font-weight:600; margin-bottom:4px;">Start dictating</div>
                <div style="font-size:13px; color:#8A8378;">Click, or press <span id="hotkeyHint" class="mono" style="font-size:11.5px; background:#F5F1EB; border:1px solid #E5DDCC; border-radius:5px; padding:2px 6px;">Ctrl + Shift + Space</span> anywhere</div>
                <div id="idleError" style="font-size:13px; color:#B0563C; margin-top:10px; min-height:0;"></div>
              </div>
            </div>

            <!-- recording -->
            <div id="recView" style="flex:1; display:none; flex-direction:column; padding:20px 4px 0;">
              <div style="display:flex; align-items:center; gap:8px; margin-bottom:12px;">
                <span style="width:8px; height:8px; border-radius:50%; background:#D9482B;"></span>
                <span class="mono" style="font-size:11px; letter-spacing:.08em; color:#8A8378; white-space:nowrap;">LISTENING — <span id="recStatus">RECORDING</span></span>
                <span class="mono" id="recElapsed" style="font-size:11px; color:#C4BBA9; margin-left:auto; white-space:nowrap;">0:00</span>
              </div>
              <div class="mono" id="recBody" style="font-size:14.5px; line-height:1.75; color:#3A362E; min-height:120px; display:flex; align-items:center; justify-content:center; text-align:center;"></div>
              <div style="font-size:12.5px; color:#B0A896; margin-top:auto; padding-top:14px;">When you finish, filler words and punctuation are cleaned up, then the text is typed into your focused app.</div>
            </div>

            <!-- done -->
            <div id="doneView" style="flex:1; display:none; flex-direction:column; padding:16px 4px 0; gap:12px;">
              <div>
                <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:5px;">
                  <div class="mono" style="font-size:10.5px; letter-spacing:.1em; color:#A69D89;">RAW TRANSCRIPT</div>
                  <button class="copybtn" id="copyRaw" title="Copy raw transcript"></button>
                </div>
                <div class="mono" id="doneRaw" style="font-size:13px; line-height:1.6; color:#8A8378;"></div>
              </div>
              <div>
                <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:5px;">
                  <div class="mono" style="font-size:10.5px; letter-spacing:.1em; color:#4CA366;">CLEANED — TYPED INTO <span id="doneApp"></span></div>
                  <button class="copybtn" id="copyClean" title="Copy cleaned text"></button>
                </div>
                <div id="doneClean" style="font-size:15px; line-height:1.65; color:#3A362E;"></div>
              </div>
              <div style="margin-top:auto; display:flex; gap:10px; align-items:center;">
                <button id="againBtn" style="background:#1F1D1A; color:#F5F1EB; border:none; border-radius:8px; padding:8px 16px; font-size:13px; font-weight:600; cursor:pointer;">New dictation</button>
                <div style="font-size:12.5px; color:#B0A896;">Saved to history below.</div>
              </div>
            </div>
          </div>

          <!-- History -->
          <div>
            <div style="display:flex; align-items:baseline; justify-content:space-between; margin:2px 2px 10px;">
              <div style="font-size:15px; font-weight:700;">History</div>
              <div class="mono" id="viewAll" style="font-size:11.5px; color:#8A8378; cursor:pointer;">view all →</div>
            </div>
            <div style="background:#FFFFFF; border:1px solid #ECE5D8; border-radius:12px; box-shadow:0 1px 3px rgba(60,50,30,.05); overflow:hidden;" id="historyBox">
              <div class="mono" style="font-size:11px; color:#B0A896; padding:22px 18px; text-align:center;">Nothing yet — start dictating above.</div>
            </div>
          </div>
        </div>

        <!-- right rail -->
        <div style="display:flex; flex-direction:column; gap:14px;">
          <div style="background:#FFFFFF; border:1px solid #ECE5D8; border-radius:12px; box-shadow:0 1px 3px rgba(60,50,30,.05); padding:16px 18px;">
            <div class="mono" style="font-size:10.5px; letter-spacing:.1em; color:#A69D89; margin-bottom:8px;">TOTAL WORDS</div>
            <div class="mono" id="totalWords" style="font-size:26px; font-weight:600; letter-spacing:-0.02em;">0</div>
            <div style="display:flex; align-items:flex-end; gap:4px; height:34px; margin-top:12px;">
              <div style="flex:1; height:40%; background:#EFE9DD; border-radius:3px 3px 0 0;"></div>
              <div style="flex:1; height:62%; background:#EFE9DD; border-radius:3px 3px 0 0;"></div>
              <div style="flex:1; height:48%; background:#EFE9DD; border-radius:3px 3px 0 0;"></div>
              <div style="flex:1; height:80%; background:#EFE9DD; border-radius:3px 3px 0 0;"></div>
              <div style="flex:1; height:55%; background:#EFE9DD; border-radius:3px 3px 0 0;"></div>
              <div style="flex:1; height:30%; background:#EFE9DD; border-radius:3px 3px 0 0;"></div>
              <div style="flex:1; height:92%; background:#1F1D1A; border-radius:3px 3px 0 0;"></div>
            </div>
            <div style="font-size:11.5px; color:#B0A896; margin-top:8px;">across all your dictations</div>
          </div>
          <div style="background:#FFFFFF; border:1px solid #ECE5D8; border-radius:12px; box-shadow:0 1px 3px rgba(60,50,30,.05); padding:16px 18px;">
            <div class="mono" style="font-size:10.5px; letter-spacing:.1em; color:#A69D89; margin-bottom:8px;">ENGINE</div>
            <div class="mono" style="font-size:13px; color:#3A362E; line-height:1.7;">faster-whisper<br>on-device · CPU int8</div>
          </div>
          <div style="background:#FFFFFF; border:1px solid #ECE5D8; border-radius:12px; box-shadow:0 1px 3px rgba(60,50,30,.05); padding:16px 18px;">
            <div class="mono" style="font-size:10.5px; letter-spacing:.1em; color:#A69D89; margin-bottom:8px;">CLEANUP</div>
            <div id="ollamaState" class="mono" style="font-size:13px; color:#3A362E; line-height:1.6;">checking Ollama…</div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- Settings modal -->
  <div id="scrim" style="display:none; position:fixed; inset:0; background:rgba(31,29,26,.28); z-index:70;"></div>
  <div id="panel" style="display:none; position:fixed; top:50%; left:50%; transform:translate(-50%,-50%); width:720px; max-width:calc(100vw - 32px); height:520px; max-height:calc(100vh - 32px); z-index:71; background:#FFFFFF; border:1px solid #ECE5D8; border-radius:16px; box-shadow:0 24px 70px rgba(30,24,12,.28); flex-direction:row; overflow:hidden; animation:panelin .2s ease-out;">
    <!-- sub-nav -->
    <div style="width:172px; flex-shrink:0; background:#FBF8F3; border-right:1px solid #F1EBDF; padding:18px 12px; display:flex; flex-direction:column; gap:2px;">
      <div class="mono" style="font-size:10px; letter-spacing:.12em; color:#A69D89; padding:0 8px 10px;">SETTINGS</div>
      <div class="subnav" data-tab="general">
        <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M2.5 5h11M2.5 11h11"></path><circle cx="6" cy="5" r="1.6" fill="#FBF8F3"></circle><circle cx="10" cy="11" r="1.6" fill="#FBF8F3"></circle></svg>General
      </div>
      <div class="subnav" data-tab="models">
        <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="1.5" width="4" height="8" rx="2"></rect><path d="M3.5 7.5a4.5 4.5 0 0 0 9 0M8 12v2.5"></path></svg>Models
      </div>
      <div class="subnav" data-tab="overlay">
        <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2 8h2M6 4v8M10 6v4M14 8h-2"></path></svg>Overlay
      </div>
      <div class="subnav" data-tab="system">
        <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="12" height="8.5" rx="1.5"></rect><path d="M5.5 14h5"></path></svg>System
      </div>
      <div style="flex:1;"></div>
      <div class="mono" style="font-size:10.5px; color:#B0A896; padding:8px;">Flow · local · offline</div>
    </div>
    <!-- content -->
    <div style="flex:1; display:flex; flex-direction:column; overflow:hidden; min-width:0;">
      <div style="padding:18px 22px 12px; border-bottom:1px solid #F1EBDF; display:flex; align-items:center; justify-content:space-between;">
        <div class="serif" id="tabTitle" style="font-size:22px; font-weight:600;">General</div>
        <button id="panelClose" style="width:28px; height:28px; border-radius:8px; border:1px solid #ECE5D8; background:#FBF8F3; color:#8A8378; cursor:pointer; display:flex; align-items:center; justify-content:center;">
          <svg width="10" height="10" viewBox="0 0 11 11" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M2 2l7 7M9 2 2 9"></path></svg>
        </button>
      </div>
      <div style="flex:1; overflow-y:auto; padding:18px 22px 24px;" id="tabBody"></div>
    </div>
  </div>
  <!-- Hotkey capture -->
  <div id="hkCapture" style="display:none; position:fixed; inset:0; z-index:80; background:rgba(31,29,26,.4); align-items:center; justify-content:center;">
    <div style="background:#FFFFFF; border-radius:16px; padding:28px 32px; width:360px; text-align:center; box-shadow:0 24px 70px rgba(30,24,12,.35);">
      <div class="serif" style="font-size:20px; font-weight:600; margin-bottom:6px;">Set shortcut</div>
      <div style="font-size:13px; color:#8A8378; margin-bottom:20px;">Press the key combination you want to use.</div>
      <div id="hkPreview" class="mono" style="font-size:17px; font-weight:600; min-height:30px; display:flex; align-items:center; justify-content:center; padding:14px; border:1.5px dashed #D8CFBC; border-radius:11px; color:#1F1D1A; background:#FBF8F3;">…</div>
      <div style="font-size:11.5px; color:#B0A896; margin-top:14px;">Press <span class="kbd">Esc</span> to cancel</div>
    </div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
const post = (url, body) => fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body: body?JSON.stringify(body):null});

const COPY_ICON='<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><rect x="5.5" y="5.5" width="8.5" height="8.5" rx="1.7"></rect><path d="M3.2 10.5h-.7a1 1 0 0 1-1-1v-7a1 1 0 0 1 1-1h7a1 1 0 0 1 1 1v.7"></path></svg>';
const CHECK_ICON='<svg width="13" height="13" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 7.3l3 3 6-7.5"></path></svg>';

function copyText(text, btn){
  if(!text) return;
  const flash = () => { if(!btn) return; const o=btn.innerHTML; btn.classList.add('copied'); btn.innerHTML=CHECK_ICON; setTimeout(()=>{ btn.classList.remove('copied'); btn.innerHTML=o; },1000); };
  const legacy = () => {   // WebView2 / cross-origin iframe blocks navigator.clipboard — fall back
    try {
      const ta=document.createElement('textarea');
      ta.value=text; ta.setAttribute('readonly','');
      ta.style.cssText='position:fixed;top:0;left:0;width:1px;height:1px;opacity:0;';
      document.body.appendChild(ta); ta.focus(); ta.select(); ta.setSelectionRange(0, text.length);
      const ok=document.execCommand('copy'); document.body.removeChild(ta);
      if(ok) flash();
    } catch(e){}
  };
  if(navigator.clipboard && navigator.clipboard.writeText && window.isSecureContext){
    navigator.clipboard.writeText(text).then(flash).catch(legacy);
  } else { legacy(); }
}

// Game-style shortcut capture: press a combo, it's set automatically.
function openHkCapture(){
  const modal = $("hkCapture"), prev = $("hkPreview");
  modal.style.display = 'flex'; prev.textContent = '…'; prev.style.color = '#1F1D1A';
  let done = false, peak = [];
  const modsOf = e => { const m=[]; if(e.ctrlKey)m.push('ctrl'); if(e.altKey)m.push('alt'); if(e.shiftKey)m.push('shift'); if(e.metaKey)m.push(/Mac/.test(navigator.platform)?'cmd':'windows'); return m; };
  const label = arr => arr.map(x=>({ctrl:'Ctrl',alt:'Alt',shift:'Shift',windows:'Win',cmd:'Cmd'}[x]||x.toUpperCase())).join(' + ');
  const normKey = e => { if(e.key===' '||e.code==='Space') return 'space';
    const m={ArrowUp:'up',ArrowDown:'down',ArrowLeft:'left',ArrowRight:'right',Enter:'enter',Tab:'tab',Backspace:'backspace',Delete:'delete'};
    return m[e.key] || e.key.toLowerCase(); };
  function finish(combo){
    if(done) return; done = true;
    document.removeEventListener('keydown', kd, true);
    document.removeEventListener('keyup', ku, true);
    modal.onclick = null;
    if(combo){ prev.textContent = label(combo.split('+')); applyHotkey(combo, modal); }
    else { modal.style.display = 'none'; }
  }
  function kd(e){
    e.preventDefault(); e.stopPropagation();
    if(e.key==='Escape'){ finish(null); return; }
    const m = modsOf(e); if(m.length > peak.length) peak = m;
    if(['Control','Alt','Shift','Meta','OS'].includes(e.key)){ prev.textContent = (label(m)||'…') + (m.length?' + …':''); }
    else { finish([...m, normKey(e)].join('+')); }
  }
  function ku(e){ e.preventDefault(); if(!done && peak.length) finish(peak.join('+')); }
  document.addEventListener('keydown', kd, true);
  document.addEventListener('keyup', ku, true);
  modal.onclick = e => { if(e.target===modal) finish(null); };
}
async function applyHotkey(combo, modal){
  const r = await (await post('/api/hotkey', {hotkey: combo})).json();
  setTimeout(()=>{ modal.style.display = 'none'; }, 450);
  if(!r.ok){ alert('Could not set "'+combo+'": '+(r.error||'invalid combination')); }
  await tick();
  if($("panel").style.display !== 'none') renderPanel();
}

$("todayDate").textContent = new Date().toLocaleDateString('en-US', {weekday:'short', month:'short', day:'numeric'});

const MODELS = [
  {name:'faster-whisper-small', size:'244 MB', speed:'9.4×', wer:'8.2%', kind:'whisper', desc:'The sweet spot for live dictation. Fast first token, solid accuracy on clean speech.'},
  {name:'faster-whisper-medium', size:'769 MB', speed:'4.1×', wer:'6.8%', kind:'whisper', desc:'Noticeably better with accents and jargon. Slower on battery.'},
  {name:'faster-whisper-large-v3', size:'1.5 GB', speed:'1.6×', wer:'5.1%', kind:'whisper', desc:'Best accuracy in the Whisper family. Great for careful re-transcription.'},
  {name:'parakeet-tdt-0.6b', size:'640 MB', speed:'12.8×', wer:'6.1%', kind:'other', tag:'trending', desc:"NVIDIA's TDT decoder — not wired up in this build."},
  {name:'moonshine-base', size:'62 MB', speed:'14.2×', wer:'9.9%', kind:'other', tag:'tiny', desc:'Tiny footprint. Not wired up in this build.'},
];
let curRow = 'faster-whisper-small';
let curTab = 'general';

function renderModels(){
  const box = $("modelList"); if(!box) return; box.innerHTML='';
  for (const m of MODELS){
    const sel = m.name===curRow;
    const dl = m.kind==='other';
    const row = document.createElement('div');
    row.style.cssText = `border:${sel?'1.5px solid #1F1D1A':'1px solid #ECE5D8'}; background:${sel?'#FBF8F3':'#FFFFFF'}; border-radius:11px; padding:13px 15px; cursor:${dl?'default':'pointer'};`;
    const status = dl ? {t:'unavailable',c:'#8A8378',bg:'#FFFFFF',bd:'#D8CFBC'} : (sel?{t:'loaded',c:'#2E7D43',bg:'#EAF4EC',bd:'#D4E8D9'}:{t:'ready',c:'#6B6353',bg:'#F5F1EB',bd:'#E5DDCC'});
    row.innerHTML = `<div style="display:flex; align-items:center; gap:8px;">
        <span class="mono" style="font-size:13px; font-weight:600;">${m.name}</span>
        ${m.tag?`<span class="mono" style="font-size:10px; color:#8A6215; background:#FAF0DC; border:1px solid #F0E1C2; border-radius:5px; padding:1.5px 6px;">${m.tag}</span>`:''}
        <span class="mono" style="font-size:10px; margin-left:auto; color:${status.c}; background:${status.bg}; border:1px solid ${status.bd}; border-radius:5px; padding:1.5px 7px;">${status.t}</span>
      </div>
      <div style="font-size:12.5px; color:#6B6353; line-height:1.45; margin-top:5px;">${m.desc}</div>
      <div class="mono" style="display:flex; gap:16px; margin-top:9px; font-size:11px; color:#8A8378;"><span>${m.size}</span><span>${m.speed} realtime</span><span>WER ${m.wer}</span></div>`;
    if (!dl) row.onclick = async () => { curRow=m.name; const r=await (await post('/api/model',{row:m.name})).json(); curRow=r.whisper_row; renderModels(); $("modelName").textContent=curRow; };
    box.appendChild(row);
  }
}

// ── settings modal ──────────────────────────────────────────────
function srow(title, sub, control){
  return `<div class="setrow" style="display:flex;align-items:center;justify-content:space-between;gap:16px;padding:15px 0;border-top:1px solid #F4EFE6;">
    <div style="min-width:0;"><div style="font-size:14px;font-weight:600;">${title}</div>${sub?`<div style="font-size:12.5px;color:#8A8378;margin-top:2px;">${sub}</div>`:''}</div>
    <div style="flex-shrink:0;display:flex;align-items:center;gap:10px;">${control}</div></div>`;
}
const card = inner => `<div style="background:#FBFAF7;border:1px solid #F1EBDF;border-radius:12px;padding:0 16px;">${inner}</div>`;
const toggle = (id,on) => `<button data-tg="${id}" style="width:42px;height:24px;border-radius:999px;border:none;cursor:pointer;position:relative;background:${on?'#1F1D1A':'#D8CFBC'};transition:background .15s;"><span style="position:absolute;top:2px;left:${on?'20px':'2px'};width:20px;height:20px;border-radius:50%;background:#fff;box-shadow:0 1px 2px rgba(0,0,0,.2);transition:left .15s;"></span></button>`;

async function renderGeneral(body){
  const s = window._state||{};
  const lang = (s.language && s.language!=='auto') ? s.language : 'auto';
  let micRes = {mics:[],current:null};
  try { micRes = await (await fetch('/api/mics')).json(); } catch(e){}
  let micOpts = '<option value="">Auto-detect (default)</option>';
  for (const m of (micRes.mics||[])) micOpts += `<option value="${m.index}">${(m.name||'').replace(/</g,'&lt;')}</option>`;
  body.innerHTML = card(
    srow('Shortcut','Press to start, press again to stop', `<span class="kbd">${(s.hotkey||'alt+space').replace(/\+/g,' + ')}</span><button class="btnchg" id="hkChange">Change</button>`) +
    srow('Microphone','Input device used for dictation', `<select class="sel" id="micSel">${micOpts}</select>`) +
    srow('Dictation language','Force a language, or auto-detect per clip', `<select class="sel" id="langSel"><option value="auto">Auto-detect</option><option value="en">English</option><option value="de">German (Deutsch)</option></select>`) +
    srow('App language','Interface language (English only in this build)', `<select class="sel" disabled><option>English</option></select>`)
  );
  $("langSel").value = lang;
  $("langSel").onchange = async e => { await post('/api/language',{value:e.target.value}); tick(); };
  $("micSel").value = micRes.current==null ? '' : String(micRes.current);
  $("micSel").onchange = async e => { await post('/api/mic',{index: e.target.value===''?null:e.target.value}); };
  $("hkChange").onclick = () => openHkCapture();
}

async function renderModelsTab(body){
  body.innerHTML =
    `<div style="font-size:13px;font-weight:700;margin:0 0 3px;">Speech model</div>
     <div style="font-size:12.5px;color:#8A8378;margin-bottom:12px;">On-device transcription · faster-whisper</div>
     <div id="modelList" style="display:flex;flex-direction:column;gap:10px;"></div>
     <div style="font-size:13px;font-weight:700;margin:22px 0 3px;">Cleanup model</div>
     <div style="font-size:12.5px;color:#8A8378;margin-bottom:10px;">Local LLM that removes fillers and adds punctuation (Ollama)</div>
     <select class="sel" id="cleanupSel" style="max-width:none;width:100%;"></select>`;
  renderModels();
  let om = {models:[],current:''};
  try { om = await (await fetch('/api/ollama_models')).json(); } catch(e){}
  const sel = $("cleanupSel");
  sel.innerHTML = (om.models&&om.models.length) ? om.models.map(m=>`<option value="${m}">${m}</option>`).join('') : `<option value="">${om.current||'Ollama offline'}</option>`;
  if(om.current) sel.value = om.current;
  sel.onchange = async e => { if(e.target.value){ await post('/api/cleanup_model',{model:e.target.value}); tick(); } };
}

const SYS=[['sys_login','Launch app at login',true],['sys_bar','Show Flow Bar at all times',false],['sys_dock','Show app in tray',true]];
const SND=[['snd_sounds','Dictation and notification sounds',true],['snd_mute','Mute music while dictating',true]];
function tgVal(id){ [...SYS,...SND].forEach(([i,,d])=>{ if(localStorage.getItem(i)===null) localStorage.setItem(i,d?'1':'0'); }); return localStorage.getItem(id)==='1'; }
function renderSystem(body){
  const mk = arr => card(arr.map(([id,label])=>srow(label,'',toggle(id,tgVal(id)))).join(''));
  body.innerHTML = `<div class="mono" style="font-size:10.5px;letter-spacing:.08em;color:#A69D89;font-weight:600;margin:0 0 8px;">APP</div>${mk(SYS)}
    <div class="mono" style="font-size:10.5px;letter-spacing:.08em;color:#A69D89;font-weight:600;margin:20px 0 8px;">SOUND</div>${mk(SND)}
    <div style="font-size:12px;color:#B0A896;margin-top:16px;line-height:1.5;">These mirror the desktop app's options and are saved in your browser, but this build is a local web app — they don't drive a system tray or audio yet.</div>`;
  body.querySelectorAll('[data-tg]').forEach(b=> b.onclick=()=>{ const id=b.dataset.tg; localStorage.setItem(id, localStorage.getItem(id)==='1'?'0':'1'); renderPanel(); });
}

const OVERLAY_PREVIEW = {
  equalizer:'<div style="display:flex;align-items:center;gap:2px;height:20px;">'+[6,12,8,16,7,11,5].map((h,i)=>`<span style="width:2px;height:${h}px;border-radius:1px;background:${i===3?'#E8912D':'#F5F1EB'};"></span>`).join('')+'</div>',
  line:'<svg width="52" height="20" viewBox="0 0 52 20"><polyline fill="none" stroke="#F5F1EB" stroke-width="2" stroke-linecap="round" points="0,10 8,5 16,13 24,7 32,12 40,6 52,10"></polyline></svg>',
  dots:'<div style="display:flex;align-items:center;gap:4px;">'+[7,10,13,10,7].map((d,i)=>`<span style="width:${d}px;height:${d}px;border-radius:50%;background:${i===2?'#E8912D':'#F5F1EB'};"></span>`).join('')+'</div>',
  orb:'<span style="width:15px;height:15px;border-radius:50%;background:#E8912D;display:block;"></span>'
};
function renderOverlay(body){
  const cur=(window._state||{}).overlay_style||'equalizer';
  const OPTS=[['equalizer','Equalizer','Balken aus der Mitte'],['line','Line wave','Wellenlinie'],['dots','Dots','Punkte'],['orb','Pulse orb','Ein Punkt']];
  body.innerHTML='<div style="font-size:12.5px;color:#8A8378;margin-bottom:14px;">How the recording pill looks. It reacts live to your voice.</div>'
    +'<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">'
    +OPTS.map(([id,name,desc])=>`<div class="ovopt" data-s="${id}" style="border:${id===cur?'1.5px solid #1F1D1A':'1px solid #ECE5D8'};background:${id===cur?'#FBF8F3':'#FFFFFF'};border-radius:11px;padding:12px;cursor:pointer;">
        <div style="background:#17150F;border-radius:9px;height:38px;display:flex;align-items:center;justify-content:center;margin-bottom:9px;">${OVERLAY_PREVIEW[id]}</div>
        <div style="font-size:13px;font-weight:600;">${name}</div>
        <div style="font-size:11.5px;color:#8A8378;">${desc}</div></div>`).join('')
    +'</div>';
  body.querySelectorAll('.ovopt').forEach(o=>o.onclick=async()=>{await post('/api/overlay_style',{style:o.dataset.s});await tick();renderPanel();});
}

function renderPanel(){
  document.querySelectorAll('.subnav').forEach(n=>n.classList.toggle('active', n.dataset.tab===curTab));
  $("tabTitle").textContent = {general:'General',models:'Models',overlay:'Overlay',system:'System'}[curTab];
  const body = $("tabBody");
  if(curTab==='general') renderGeneral(body);
  else if(curTab==='models') renderModelsTab(body);
  else if(curTab==='overlay') renderOverlay(body);
  else renderSystem(body);
}

const openPanel = () => { $("scrim").style.display='block'; $("panel").style.display='flex'; renderPanel(); };
$("modelBtn").onclick = () => { curTab='models'; openPanel(); };
$("settingsBtn").onclick = () => { curTab='general'; openPanel(); };
$("panelClose").onclick = $("scrim").onclick = () => { $("scrim").style.display='none'; $("panel").style.display='none'; };
document.querySelectorAll('.subnav').forEach(n=> n.onclick=()=>{ curTab=n.dataset.tab; renderPanel(); });

$("micBtn").onclick = () => post('/api/start');
$("againBtn").onclick = () => post('/api/reset');
$("copyRaw").innerHTML = COPY_ICON; $("copyClean").innerHTML = COPY_ICON;
$("copyRaw").onclick = function(){ copyText((window._state||{}).raw||'', this); };
$("copyClean").onclick = function(){ copyText((window._state||{}).clean||'', this); };
$("viewAll").onclick = () => { window._histExpanded = !window._histExpanded; renderHistory((window._state||{}).history||[]); };

function show(view){
  $("idleView").style.display = view==='idle'?'flex':'none';
  $("recView").style.display  = view==='rec'?'flex':'none';
  $("doneView").style.display = view==='done'?'flex':'none';
}

const HIST_CAP = 6;
function renderHistory(h){
  const box = $("historyBox"), va = $("viewAll");
  if (!h.length){
    box.innerHTML='<div class="mono" style="font-size:11px; color:#B0A896; padding:22px 18px; text-align:center;">Nothing yet — start dictating above.</div>';
    if(va) va.style.display='none';
    return;
  }
  window._histTexts = h.map(r=>r.text);
  const expanded = !!window._histExpanded;
  const show = expanded ? h : h.slice(0, HIST_CAP);
  if(va){
    if(h.length > HIST_CAP){ va.style.display='block'; va.textContent = expanded ? 'show less ↑' : `view all (${h.length}) →`; }
    else va.style.display='none';
  }
  const today = new Date().toISOString().slice(0,10);
  let html=''; let lastDay=null;
  show.forEach((r, i) => {
    const label = r.day===today ? 'TODAY' : r.day;
    if (label!==lastDay){ html += `<div class="mono" style="font-size:10.5px; letter-spacing:.1em; color:#A69D89; padding:${lastDay?'14':'12'}px 18px 6px; ${lastDay?'border-top:1px solid #F4EFE6;':''}">${label}</div>`; lastDay=label; }
    const t = r.text.replace(/</g,'&lt;');
    html += `<div class="row" style="display:flex; align-items:center; gap:14px; padding:10px 18px; border-top:1px solid #F4EFE6;">
      <span class="mono" style="font-size:11.5px; color:#8A8378; width:62px; flex-shrink:0;">${r.time}</span>
      <span style="font-size:13.5px; color:#3A362E; flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${t}</span>
      <span class="mono" style="font-size:11px; color:#B0A896; flex-shrink:0;">→ ${r.app}</span>
      <button class="copybtn" data-ci="${i}" title="Copy to clipboard">${COPY_ICON}</button></div>`;
  });
  box.innerHTML = html;
  box.querySelectorAll('[data-ci]').forEach(b => b.onclick = function(){ copyText(window._histTexts[+this.dataset.ci], this); });
}

let lastStatus=null;
async function tick(){
  let s; try { s = await (await fetch('/api/state')).json(); } catch(e){ return; }
  window._state = s;
  const hkFmt = (s.hotkey||'').split('+').map(x=>({ctrl:'Ctrl',alt:'Alt',shift:'Shift',windows:'Win',cmd:'Cmd',space:'Space'}[x]||x.toUpperCase())).join(' + ');
  const hh = $("hotkeyHint"); if(hh) hh.textContent = hkFmt || 'Ctrl + Shift + Space';
  curRow = s.whisper_row; $("modelName").textContent = s.whisper_row;
  const forced = s.language && s.language !== 'auto';
  const lang = forced ? s.language : (s.detected_lang || 'auto');
  $("engineLine").innerHTML = `local · <span id="langPill" title="Click to pin the language (auto / en / de)" style="cursor:pointer; border-bottom:1px dotted #B0A896; padding-bottom:1px;">${lang}${forced ? '' : ' 🔍'}</span> · cleanup: ${s.cleanup_model}`;
  $("langPill").onclick = async (e) => { e.stopPropagation(); await post('/api/language'); tick(); };
  $("totalWords").textContent = (s.total_words||0).toLocaleString();
  const setup = s.ollama_setup || '';
  $("ollamaState").innerHTML = setup==='running'
     ? `<span style="color:#8A8378;">setting up Ollama…</span>`
     : s.ollama_ok===false
     ? `<span style="color:#B0563C;">Ollama offline</span><br><span style="color:#8A8378;">raw transcript used</span>`
       + (setup.startsWith('error') ? `<br><span style="color:#B0563C;">${setup.replace(/</g,'&lt;')}</span>` : '')
       + `<br><a href="#" onclick="post('/api/ollama_setup');return false;" style="color:#E8912D;">set up cleanup</a>`
     : `<span style="color:#2E7D43;">●</span> ${s.cleanup_model}`;
  renderHistory(s.history||[]);

  if (s.status==='recording'){
    show('rec'); $("recStatus").textContent='RECORDING';
    $("recBody").innerHTML = '<span style="color:#B0A896;">Listening… speak now, then press your shortcut again (or ✓ in the floating bar) to finish.</span>';
    const el = Math.max(0, Math.floor(Date.now()/1000 - s.started));
    $("recElapsed").textContent = Math.floor(el/60)+':'+String(el%60).padStart(2,'0');
  } else if (s.status==='transcribing' || s.status==='cleaning'){
    show('rec');
    $("recStatus").textContent = s.status==='transcribing' ? 'TRANSCRIBING' : 'CLEANING UP';
    $("recBody").innerHTML = `<span style="display:inline-block;width:16px;height:16px;border:2px solid #E5DDCC;border-top-color:#E8912D;border-radius:50%;animation:spin .7s linear infinite;margin-right:10px;vertical-align:-3px;"></span>${s.status==='transcribing'?'Transcribing your audio…':'Cleaning up with '+s.cleanup_model+'…'}`;
  } else if (s.status==='done'){
    show('done');
    $("doneRaw").textContent = s.raw || '—';
    $("doneClean").textContent = s.clean || '—';
    $("doneApp").textContent = s.app || 'app';
  } else {
    show('idle');
    $("idleError").textContent = s.error
      || (s.hotkey_ok===false ? 'Global shortcut unavailable — use the mic button (try launching as administrator).' : '');
  }
  lastStatus = s.status;
}
setInterval(tick, 400); tick();
</script>
</body></html>"""


def selftest():
    print("Flow self-test — verifies the ASR + cleanup pipeline (no mic needed).\n")
    ok = True

    # 1. imports
    try:
        import sounddevice  # noqa
        if sys.platform == "win32":
            import keyboard  # noqa
        from faster_whisper import WhisperModel  # noqa
        print("  [ok] imports: sounddevice, keyboard, faster-whisper")
    except Exception as e:
        print(f"  [FAIL] import: {e}")
        return False

    # 2. transcribe a sentence spoken by Kokoro (the TTS tool is bundled alongside)
    try:
        import tempfile
        import soundfile as sf
        import app as tts
        spoken = [r.audio for r in tts.get_pipeline("a")(
            "Hello! This is Kokoro running locally on my PC.", voice="af_heart")]
        sample = Path(tempfile.mkdtemp()) / "sample.wav"
        sf.write(sample, np.concatenate([np.asarray(a, dtype=np.float32) for a in spoken]),
                 tts.SAMPLE_RATE)  # a path, so faster-whisper resamples 24 → 16 kHz itself
        t0 = time.time()
        text, _ = transcribe_seg(get_whisper(WHISPER_SIZES[settings["whisper_row"]]), str(sample), None)
        print(f"  [ok] transcribe Kokoro sample ({time.time()-t0:.1f}s): {text!r}")
        assert "kokoro" in text.lower(), "expected 'Kokoro' in the sample transcript"
    except Exception as e:
        print(f"  [FAIL] transcribe: {e}")
        ok = False

    # 3. cleanup via Ollama (if reachable)
    raw = "um so i i think we should uh move the launch to thursday you know because the build still needs qa"
    if ollama_up():
        try:
            clean = cleanup(raw)
            print(f"  [ok] Ollama cleanup: {clean!r}")
            assert clean and clean != raw, "cleanup returned the raw text unchanged"
            assert "um" not in clean.lower().split(), "filler 'um' survived"
        except Exception as e:
            print(f"  [FAIL] cleanup: {e}")
            ok = False
    else:
        print(f"  [skip] Ollama not reachable at {OLLAMA_URL} — cleanup would fall back to raw text")

    # 4. history round-trip
    try:
        before = len(load_history())
        add_history("self-test entry", "pytest")
        assert len(load_history()) == before + 1
        h = load_history()
        h = [x for x in h if x["text"] != "self-test entry"]
        HIST_FILE.write_text(json.dumps(h, indent=2, ensure_ascii=False), encoding="utf-8")
        print("  [ok] history add/load round-trip")
    except Exception as e:
        print(f"  [FAIL] history: {e}")
        ok = False

    print("\n" + ("PASS" if ok else "FAIL"))
    return ok


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # Windows console defaults to cp1252
    except Exception:
        pass
    if "--selftest" in sys.argv:
        raise SystemExit(0 if selftest() else 1)

    import socket
    with socket.socket() as _s:  # Werkzeug swallows bind errors and sys.exits itself, so probe first
        if _s.connect_ex(("127.0.0.1", 7600)) == 0:
            raise SystemExit("\n  [!] Port 7600 is already in use - is Flow already running?\n"
                             "      Close the other instance and try again.\n")

    hotkey_ok = start()
    url = "http://127.0.0.1:7600"
    print(f"\n  Flow → {url}")
    print(f"  Hotkey:  {settings['hotkey']} ({'active' if hotkey_ok else 'unavailable — use the mic button'})")
    print(f"  Cleanup: Ollama {settings['cleanup_model']} ({'up' if state['ollama_ok'] else 'offline → raw transcript'})\n")
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    if sys.platform == "darwin":  # Carbon hotkey events need a Cocoa run loop on the main thread
        from PyObjCTools import AppHelper
        threading.Thread(target=app.run, kwargs=dict(host="127.0.0.1", port=7600), daemon=True).start()
        AppHelper.runEventLoop(installInterrupt=True)
    else:
        app.run(host="127.0.0.1", port=7600, debug=False)
