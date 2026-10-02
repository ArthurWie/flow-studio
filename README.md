# Flow Studio

A local, offline desktop app bundling two tools in one window:

- **Dictation** — press a hotkey anywhere, speak, and cleaned-up text is typed into whatever app you're in. Speech → text via [faster-whisper](https://github.com/SYSTRAN/faster-whisper), filler-word/punctuation cleanup via a local LLM.
- **Text to Speech** — the [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) model with streaming playback, word-level highlighting, voice blending, and MP3 export.

Everything runs on your machine. After the first model download, nothing leaves your PC.

> **Platform: Windows only.** The dictation tool uses Win32 APIs (global hotkey, clipboard, focus) via `ctypes.windll` and the desktop shell uses Edge WebView2. The TTS tool (`app.py`) alone is cross-platform, but the bundled app is not.

---

## For users (installed build)

1. Download **FlowStudioSetup.exe** from the Releases page and run it. It installs per-user (no admin) and adds a "Flow Studio" shortcut.
2. Launch **Flow Studio**. The first launch shows a one-time **Setup** screen — click **Install** and it downloads everything automatically (~1.7 GB): the engine, then the voices + speech recognition. Needs internet this once; after that the app runs offline.
3. Optionally enable **dictation cleanup** on the setup screen — it installs Ollama and the `qwen2.5:3b` model for you. Dictation works without it (it just types the raw transcript).

The download is split across PyPI (engine) and Hugging Face (models); nothing large is hosted by the project. Setup is resumable — if it's interrupted, relaunch and it picks up where it left off.

**Recommended extras** — the app runs without them, but degrades:

| Extra | What you lose without it | Install |
| --- | --- | --- |
| **WebView2 Runtime** | The native window — the app opens in your default browser instead | Preinstalled on Windows 11; [download for Win10](https://developer.microsoft.com/microsoft-edge/webview2/) |
| **ffmpeg** | MP3 export in the TTS tool (WAV still works) | [ffmpeg.org](https://ffmpeg.org), put `ffmpeg.exe` on PATH |

### The LLM cleanup, briefly

Dictation cleanup runs **`qwen2.5:3b` on your local Ollama server** (`127.0.0.1:11434`) — it strips filler words and fixes punctuation without rephrasing. `qwen2.5:3b` is the default *model*; Ollama is the *server* that runs it. The setup screen's optional step installs both for you; if you skip it (or Ollama isn't running), dictation still works, just with the raw transcript typed out. You can switch models in the app's settings (any Ollama model works; instruction-tuned 3B models are the sweet spot).

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

### On the Mac (dev runs)

`python3 flow_studio.py` gives a Dock icon instead of a tray. Closing the window hides it while dictation keeps running; click the Dock icon to bring it back, and press Cmd+Q to quit. Dictation pops a floating overlay that never takes focus from the app you're typing into.

macOS grants the microphone and Accessibility permissions to the app that launched Python, not to Python. In a dev run that's your terminal (Terminal, iTerm, the VS Code terminal…):

- **Microphone:** the first dictation shows the system prompt for the terminal. If you missed it, allow the terminal under System Settings → Privacy & Security → Microphone and restart it.
- **Accessibility** (paste): the first paste opens the prompt; allow the terminal under Privacy & Security → Accessibility.

The frozen `.app` (phase 3) will ask for these itself; its `Info.plist` needs `NSMicrophoneUsageDescription`, e.g. "Flow Studio listens to your microphone while dictation is on, and turns your speech into text on this Mac."

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

Two build paths, depending on whether the target machine has internet access for first launch.

### Primary: the lightweight installer

```powershell
.\package.ps1     # builds bootstrap.exe, fetches uv, stages files, runs Inno Setup
```

Requires **Inno Setup 6** (`ISCC.exe` on PATH, in its Program Files location, or the per-user `%LOCALAPPDATA%\Programs\Inno Setup 6` — `package.ps1` checks all three). Under the hood it: freezes `bootstrap.py` to `bootstrap.exe` with PyInstaller (`--onefile --noconsole` — stdlib-only, so it packs small and fast), fetches `uv.exe`, stages `flow_studio.py` / `app.py` / `flow.py` / `os_win.py` / `paths.py` / `flow.ico` / `requirements.txt` / `bootstrap.exe` / `uv.exe`, then compiles `FlowStudio.iss`.

Output: **`FlowStudioSetup.exe`** (~26 MB, LZMA-compressed over an ~85 MB payload), installing per-user (no admin) to `%LOCALAPPDATA%\Programs\FlowStudio`. On first launch, `bootstrap.exe` runs `uv venv` + `uv pip install` against `requirements.txt` and warms the models — see [For users](#for-users-installed-build) above for the user-facing flow.

### Alternative: full offline bundle (for air-gapped installs)

For machines without internet access, or to skip the first-run download entirely, `build.ps1` still produces the older **PyInstaller `--onedir` build** with everything pre-baked (~1.1 GB). `onedir`, not `onefile` — `onefile` unpacks torch to a temp dir on every launch and is painfully slow.

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

Build with `-Debug` (console) first so startup errors are visible; switch to the default windowed build (matches `pythonw` shipping) once it's clean. To wrap this build in its own installer, point Inno Setup at `dist\FlowStudio\`: install to a per-user location (`{localappdata}\Flow Studio`), create a Start-menu + desktop shortcut using `flow.ico`, and register an uninstaller — `FlowStudio.iss` is a working example to adapt.

### Sign the executable

The app registers a **global hotkey and pastes via the clipboard** — behavior antivirus/SmartScreen associate with keyloggers. An unsigned exe will trigger a SmartScreen warning on download. Code-sign the installer (and ideally `bootstrap.exe`/`FlowStudio.exe` inside it) to avoid it.

### Deployment gotchas

- **User-data paths.** History, the debug log, pronunciations, and TTS output write to the per-OS data dir from `paths.data_dir()` (used by `app.py`, `flow.py` and `bootstrap.py`), not next to the code — safe under a read-only install dir. Bundled read-only resources (the HTML) still load from the install dir via `BASE_DIR`.
- **Ports 7500/7600 are still fixed**, but a collision is now handled: each entry point probes the port before binding and reports it cleanly — a console message for `app.py`/`flow.py`, a Windows dialog for `flow_studio.py` (which runs under `pythonw`, no console). Werkzeug swallows bind errors and `sys.exit`s inside its own thread, so the pre-bind probe is the reliable place to catch this. Auto-selecting a free port is a possible future improvement.
- **Ollama is separate.** It can't be bundled sanely — it's its own installer and background service. The lightweight installer's setup screen can install it for you (the optional "dictation cleanup" step); the offline bundle has no such step, so point users at ollama.com instead.

---

## Data & privacy

All processing is local. Files are written to `%LOCALAPPDATA%\FlowStudio\` (Windows), `~/Library/Application Support/FlowStudio/` (macOS) or `$XDG_DATA_HOME/flow-studio/`, default `~/.local/share/flow-studio/` (Linux):

- `settings.json` — dictation settings (hotkey, mic, Whisper model, cleanup model, language, overlay style).
- `flow_history.json` — your dictated text (last 200 entries). Plaintext.
- `flow_debug.log` — diagnostic log, appended to every run.
- `pronunciations.json` — your TTS pronunciation overrides.
- `outputs/` — generated TTS `.wav`/`.mp3` files (`outputs/_chunks/` is scratch, cleared on startup).

No telemetry, no network calls except the one-time setup downloads (PyPI for the engine, Hugging Face for the models).
