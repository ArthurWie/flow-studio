# Flow Studio

A local, offline desktop app bundling two tools in one window:

- **Dictation** — press a hotkey anywhere, speak, and cleaned-up text is typed into whatever app you're in. Speech → text via [faster-whisper](https://github.com/SYSTRAN/faster-whisper), filler-word/punctuation cleanup via a local LLM.
- **Text to Speech** — the [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) model with streaming playback, word-level highlighting, voice blending, and MP3 export.

Everything runs on your machine. After the first model download, nothing leaves your PC.

> **Platform: Windows only.** The dictation tool uses Win32 APIs (global hotkey, clipboard, focus) via `ctypes.windll` and the desktop shell uses Edge WebView2. The TTS tool (`app.py`) alone is cross-platform, but the bundled app is not.

---

## For users (installed build)

Download and run the installer. It creates a Start-menu shortcut ("Flow Studio").

**Recommended extras** — the app runs without them, but degrades:

| Extra | What you lose without it | Install |
| --- | --- | --- |
| **Ollama** + a model | Transcript cleanup — dictation falls back to the *raw* transcript (fillers, no punctuation) | [ollama.com](https://ollama.com), then `ollama pull qwen2.5:3b` |
| **WebView2 Runtime** | The native window — the app opens in your default browser instead | Preinstalled on Windows 11; [download for Win10](https://developer.microsoft.com/microsoft-edge/webview2/) |
| **ffmpeg** | MP3 export in the TTS tool (WAV still works) | [ffmpeg.org](https://ffmpeg.org), put `ffmpeg.exe` on PATH |

**First launch needs an internet connection** — it downloads the Kokoro TTS model (~330 MB) and the Whisper ASR model (~244 MB for `small`) once, then works fully offline.

### The LLM cleanup, briefly

Dictation cleanup runs **`qwen2.5:3b` on your local Ollama server** (`127.0.0.1:11434`) — it strips filler words and fixes punctuation without rephrasing. `qwen2.5:3b` is the default *model*; Ollama is the *server* that runs it. If Ollama isn't running, you still get the raw transcript typed out. You can switch models in the app's settings (any Ollama model works; instruction-tuned 3B models are the sweet spot).

---

## For developers

Requires **Python 3.12** (tested; 3.10+ likely fine) on Windows.

```powershell
py -3.12 -m venv venv
venv\Scripts\pip install -r requirements.txt
```

Then, in a separate terminal, make sure Ollama is running with a model:

```powershell
ollama pull qwen2.5:3b
```

### Run

```powershell
# The full bundled app (both tools in one native window):
venv\Scripts\python flow_studio.py
venv\Scripts\pythonw flow_studio.py     # no console window (what the shortcut uses)

# Or each tool standalone in the browser:
venv\Scripts\python app.py              # Text to Speech  → http://127.0.0.1:7500
venv\Scripts\python flow.py             # Dictation       → http://127.0.0.1:7600
```

### Self-tests (no mic needed)

```powershell
venv\Scripts\python flow.py --selftest          # verifies ASR + Ollama cleanup + history
venv\Scripts\python flow_studio.py --selftest   # starts both servers, checks they answer
```

### How it fits together

| File | Role |
| --- | --- |
| `flow_studio.py` | Desktop shell — starts both Flask servers, shows them in one WebView2 window with tabs, drives the floating dictation overlay |
| `app.py` | Kokoro TTS server (Flask, port 7500) + its web UI |
| `flow.py` | Dictation server (Flask, port 7600): mic capture → faster-whisper → Ollama cleanup → types into the focused app; global hotkey via Win32 `RegisterHotKey` |

---

## Building for deployment

Target shape: a **PyInstaller `--onedir` build wrapped in an Inno Setup installer**. `onedir`, not `onefile` — `onefile` unpacks torch to a temp dir on every launch and is painfully slow.

### 1. Bundle with PyInstaller — verified

```powershell
venv\Scripts\pip install pyinstaller
.\build.ps1            # windowed (production);  .\build.ps1 -Debug for a console build
```

[`build.ps1`](build.ps1) encodes a **verified** flag set: the resulting `FlowStudio.exe` loads torch + kokoro + misaki + spaCy and generates audio end-to-end. Output is `<drive>:\FlowStudio-build\dist\FlowStudio\` — a **~1.1 GB** `onedir` folder (torch is 470 MB of it).

**What actually fought the freezer (spike findings):** *not torch* — its PyInstaller hooks work out of the box. The real work was data-file collection for the NLP stack, which is why the flag list is long:

- **spaCy `en_core_web_sm`** — `misaki` calls `spacy.load("en_core_web_sm")` and, if `spacy.util.is_package()` can't see it, tries to *download it at runtime* (fatal in a frozen app). Fixed with `--collect-all en_core_web_sm` **plus `--copy-metadata en_core_web_sm spacy`** so it's detected, not re-downloaded.
- **`language_tags`** — ships a `data/json/` tree loaded via `importlib.resources`; needs `--collect-all language_tags` (this one only surfaced at generation time, not import time).
- **Native-DLL packages** — `sounddevice`, `soundfile`, `ctranslate2`, `av`, `onnxruntime`, `espeakng_loader` each carry binaries; all `--collect-all`'d.
- **Cross-drive `--specpath` bug** — PyInstaller can't compute a relative path from a spec on one drive to a script on another (`ValueError: path is on mount 'D:'`). `build.ps1` keeps the build dir on the repo's drive.

Build with `-Debug` (console) first so startup errors are visible; switch to the default windowed build (matches `pythonw` shipping) once it's clean.

### 2. Wrap in an installer (Inno Setup)

Point [Inno Setup](https://jrsoftware.org/isinfo.php) at `dist\Flow Studio\`. Install to a **per-user** location (`{localappdata}\Flow Studio`), create a Start-menu + desktop shortcut using `flow.ico`, and register an uninstaller.

### 3. Sign the executable

The app registers a **global hotkey and pastes via the clipboard** — behavior antivirus/SmartScreen associate with keyloggers. An unsigned exe will trigger a SmartScreen warning on download. Code-sign the exe (and ideally the installer) to avoid it.

### Deployment gotchas

- **User-data paths.** History, the debug log, pronunciations, and TTS output write to `%LOCALAPPDATA%\FlowStudio` (`DATA_DIR` in both `app.py` and `flow.py`), not next to the code — safe under a read-only install dir. Bundled read-only resources (HTML, the `output_0.wav` test sample) still load from the install dir via `BASE_DIR`.
- **Ports 7500/7600 are still fixed**, but a collision is now handled: each entry point probes the port before binding and reports it cleanly — a console message for `app.py`/`flow.py`, a Windows dialog for `flow_studio.py` (which runs under `pythonw`, no console). Werkzeug swallows bind errors and `sys.exit`s inside its own thread, so the pre-bind probe is the reliable place to catch this. Auto-selecting a free port is a possible future improvement.
- **Ollama is separate.** It can't be bundled sanely — it's its own installer and background service. The installer should detect it and link the user to it, not try to ship it.

---

## Data & privacy

All processing is local. Files are written to `%LOCALAPPDATA%\FlowStudio\`:

- `flow_history.json` — your dictated text (last 200 entries). Plaintext.
- `flow_debug.log` — diagnostic log, appended to every run.
- `pronunciations.json` — your TTS pronunciation overrides.
- `outputs/` — generated TTS `.wav`/`.mp3` files (`outputs/_chunks/` is scratch, cleared on startup).

No telemetry, no network calls except the one-time model downloads from Hugging Face.
