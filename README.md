# Flow Studio

A local, offline desktop app bundling two tools in one window:

- **Dictation** — press a hotkey anywhere, speak, and cleaned-up text is typed into whatever app you're in. Speech → text via [faster-whisper](https://github.com/SYSTRAN/faster-whisper), filler-word/punctuation cleanup via a local LLM.
- **Text to Speech** — the [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) model with streaming playback, word-level highlighting, voice blending, and MP3 export.

Everything runs on your machine; your text and audio never leave it. The network is used only for the update check at launch (you can turn it off) and for what you ask for: an update, a model download, the Ollama setup, a URL you paste.

> **Platform: Windows only.** The dictation tool uses Win32 APIs (global hotkey, clipboard, focus) via `ctypes.windll` and the desktop shell uses Edge WebView2. The TTS tool (`app.py`) alone is cross-platform, but the bundled app is not.

---

## For users (installed build)

1. Download **FlowStudioSetup.exe** from the [Releases](../../releases) page and run it. It installs per-user (no admin) and adds a "Flow Studio" shortcut. It's unsigned for now, so SmartScreen asks once: **More info → Run anyway**.
   On an Apple-silicon Mac (macOS 14+): download **FlowStudio.dmg**, drag **Flow Studio** to Applications. It's signed ad-hoc only (not notarized), so the first launch is blocked: System Settings → Privacy & Security → **Open Anyway**. It asks for the microphone on the first dictation and for Accessibility on the first paste.
   On Linux (x86_64, glibc 2.39+: Ubuntu 24.04, Fedora 40 or newer): download **flow-studio-linux-x86_64.tar.gz**, then `tar xzf flow-studio-linux-x86_64.tar.gz && ./flow-studio/install-linux.sh`. No root needed: it installs to `~/.local/share/flow-studio/app`, adds the `flow-studio` command to `~/.local/bin` and a launcher to the app menu, and if a system library is missing (PortAudio for dictation, a few X/Qt libraries for the window) it prints the `sudo apt install …` / `sudo dnf install …` line that adds it. The app updates itself (see **Updates** below); running the script from a newer tarball updates by hand. Your data in `~/.local/share/flow-studio` stays. To uninstall: `~/.local/share/flow-studio/app/install-linux.sh --uninstall` removes the app, the command and the launcher, and keeps your data (history, settings, models) in `~/.local/share/flow-studio`; delete that folder to remove it too. For the dictation shortcut see [On Linux](#on-linux-dev-runs).
2. Launch **Flow Studio**. Everything ships in the installer — the engine and the default models (Kokoro-82M, Whisper `small`) — so there's no setup step and no download.
3. Optionally click **set up cleanup** in the dictation dashboard's Cleanup card — it installs Ollama (only if its installer — on the Mac, its app — is validly signed) and pulls `qwen2.5:3b`. Dictation works without it (it just types the raw transcript).

**Updates:** at launch the app asks GitHub Releases whether there's a newer version (Settings → General → *Check for updates* turns that off). If there is, the dashboard shows **Install**; it downloads with progress (an interrupted download resumes at the next launch), checks the release's ed25519 signature and the file's SHA-256, waits until no dictation or speech is running, then installs silently and restarts. On the Mac it swaps the `.app` in place, so it needs write access to the folder the app is in. On Linux it runs the new tarball's `install-linux.sh`.

Installing over an older Flow Studio upgrades it in place: the old `env\` folder is removed; history, pronunciations and settings in `%LOCALAPPDATA%\FlowStudio` are kept. The installer adds the **WebView2 Runtime** if it's missing (preinstalled on Windows 11).

**Recommended extra** — **ffmpeg** for MP3 export in the TTS tool (WAV works without it): [ffmpeg.org](https://ffmpeg.org), put `ffmpeg.exe` on PATH.

### The LLM cleanup, briefly

Dictation cleanup runs **`qwen2.5:3b` on your local Ollama server** (`127.0.0.1:11434`) — it strips filler words and fixes punctuation without rephrasing. `qwen2.5:3b` is the default *model*; Ollama is the *server* that runs it. The dashboard's **set up cleanup** link installs both for you; if you skip it (or Ollama isn't running), dictation still works, just with the raw transcript typed out. You can switch models in the app's settings (any Ollama model works; instruction-tuned 3B models are the sweet spot).

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

The frozen `.app` asks for these itself (its `Info.plist` carries `NSMicrophoneUsageDescription`, from `FlowStudio.spec`).

### On Linux (dev runs)

Install the desktop helpers from your distro: `xdotool` and `xclip` on X11 (also WSLg), `wl-clipboard` on Wayland, plus `ydotool` if you want Wayland to paste for you. Without a key-sending tool the text lands on the clipboard and Flow asks you to press Ctrl+V.

On X11 the hotkey from Settings works as on Windows, hold-to-talk included. Wayland doesn't let apps grab keys, so bind the toggle command to a keyboard shortcut instead (it works on X11 too). Each run starts or stops dictation in the running app:

```bash
python3 /path/to/flow_studio.py toggle    # the packaged app: flow-studio toggle
```

- **GNOME:** Settings → Keyboard → View and Customize Shortcuts → Custom Shortcuts → **+**. Name it "Flow dictation", paste the command, set the shortcut.
- **KDE Plasma 6:** System Settings → Keyboard → Shortcuts → **Add New** → **Command or Script…**, paste the command, then set the shortcut. (Plasma 5: System Settings → Shortcuts → Custom Shortcuts → Edit → New → Global Shortcut → Command/URL.)
- **Cinnamon:** System Settings → Keyboard → Shortcuts → **Add custom shortcut**, paste the command, then click the keyboard-binding row and press the shortcut.

On Wayland the app can't see which window is focused, so dictation pastes into whatever has focus when it finishes.

### Self-tests (no mic needed)

```powershell
venv\Scripts\python flow.py --selftest          # verifies ASR + Ollama cleanup + history
venv\Scripts\python flow_studio.py --selftest   # both servers answer + Kokoro speaks a sentence, Whisper must hear it
```

The frozen build's `--selftest` has no console: it writes to `flow_studio.log` in the data dir and reports through its exit code, naming the step that broke.

### How it fits together

| File | Role |
| --- | --- |
| `flow_studio.py` | Desktop shell — starts both Flask servers, shows them in one WebView2 window with tabs, drives the floating dictation overlay |
| `app.py` | Kokoro TTS server (Flask, port 7500) + its web UI |
| `flow.py` | Dictation server (Flask, port 7600): mic capture → faster-whisper → Ollama cleanup → types into the focused app; global hotkey via Win32 `RegisterHotKey` |

---

## Building for deployment

CI builds the installer ([`.github/workflows/release.yml`](.github/workflows/release.yml), `windows-latest`): it installs `requirements-win.lock`, runs `package.ps1`, installs the result silently, runs `FlowStudio.exe --selftest` with the app's outbound network blocked by a firewall rule, and records the sizes in the job summary. Pushing a `v*` tag publishes `FlowStudioSetup.exe` to GitHub Releases (artifacts are too big).

A second job does the same on `macos-14` (arm64) with `requirements-mac.lock` and `package.sh` → **`FlowStudio.dmg`**; the selftest runs under `sandbox-exec` with outbound network denied except loopback. It runs only on `v*` tags and `gh workflow run release --ref <branch>`, never on PRs: Mac minutes count 10× on a private repo. Locally on the Mac: same two `uv` lines with `requirements-mac.lock` and `venv/bin/python`, then `./package.sh`. The `.dmg` is ad-hoc signed; the Developer ID + notarization steps for a public release are in `package.sh`'s header.

Linux has its own workflow ([`release-linux.yml`](.github/workflows/release-linux.yml), `ubuntu-24.04`, pinned because the build host’s glibc is the minimum, also on PRs that touch the Linux build): `requirements-linux.lock` (CPU torch: install it with `uv pip install --torch-backend cpu -r requirements-linux.lock`), `package-linux.sh` → **`flow-studio-linux-x86_64.tar.gz`**, then `install-linux.sh`, a check that the Qt window opens under Xvfb, `--selftest` in a network namespace with only loopback, and a bare `ubuntu:24.04` container where the apt line `install-linux.sh` prints must install everything it reported missing. The window is pywebview's Qt backend (PySide6, bundled): a bundled WebKitGTK wouldn't find its helper processes across distros.

A last job, on tags only, downloads both installers from the release (plus the Linux tarball, once release-linux.yml has published it: it polls the release for up to 100 minutes), writes **`latest.json`** (version, URL, SHA-256 and size per OS) and signs it with the `UPDATE_SIGNING_KEY` repo secret (`latest.json.sig`); the app verifies it against the public key in `updater.py`. Make the key pair once with `uv run --with cryptography python updater.py keygen`.

Locally, on Windows:

```powershell
uv venv --python 3.12 venv
uv pip install --python venv\Scripts\python.exe -r requirements-win.lock pyinstaller==6.22.3
.\package.ps1            # windowed (production);  .\package.ps1 -Debug for a console build
```

Requires **Inno Setup 6** (`ISCC.exe` on PATH, in its Program Files location, or the per-user `%LOCALAPPDATA%\Programs\Inno Setup 6`). `package.ps1`:

1. freezes the app with [`FlowStudio.spec`](FlowStudio.spec) → `dist\FlowStudio\` (PyInstaller `--onedir`; `onefile` unpacks torch to a temp dir on every launch),
2. downloads the default models into `dist\FlowStudio\models\` as a Hugging Face cache (the frozen app points `HF_HOME` there),
3. fetches the WebView2 Evergreen bootstrapper,
4. compiles `FlowStudio.iss` → **`FlowStudioSetup.exe`**, installing per-user (no admin) to `%LOCALAPPDATA%\Programs\FlowStudio`.

`requirements-win.lock` pins the full tree for reproducible builds. Regenerate it after changing `requirements.txt`:
`uv pip compile requirements.txt --python-version 3.12 --python-platform x86_64-pc-windows-msvc -o requirements-win.lock`
(Mac: `MACOSX_DEPLOYMENT_TARGET=14.0 uv pip compile requirements.txt --python-version 3.12 --python-platform aarch64-apple-darwin -o requirements-mac.lock`; torch 2.13 has no wheel below macOS 14.
Linux: `uv pip compile requirements.txt --python-version 3.12 --python-platform x86_64-manylinux_2_28 --torch-backend cpu -o requirements-linux.lock`; without `--torch-backend cpu` PyPI's torch pulls ~3 GB of CUDA libraries).

**What fought the freezer (spike findings):** *not torch* — its PyInstaller hooks work out of the box. The real work was data-file collection for the NLP stack, which is why the spec's `collect_all` list is long:

- **spaCy `en_core_web_sm`** — `misaki` calls `spacy.load("en_core_web_sm")` and, if `spacy.util.is_package()` can't see it, tries to *download it at runtime* (fatal in a frozen app). Fixed by collecting `en_core_web_sm` **plus copying the `en_core_web_sm` and `spacy` metadata** so it's detected, not re-downloaded.
- **`language_tags`** — ships a `data/json/` tree loaded via `importlib.resources`; needs collecting (this one only surfaced at generation time, not import time).
- **Native-DLL packages** — `sounddevice`, `soundfile`, `ctranslate2`, `av`, `onnxruntime`, `espeakng_loader` each carry binaries; all collected.

Build with `-Debug` (console) first so startup errors are visible.

### Sign the executable

The app registers a **global hotkey and pastes via the clipboard** — behavior antivirus/SmartScreen associate with keyloggers. The installer is unsigned for now, so SmartScreen warns on download. Code-sign the installer and `FlowStudio.exe` to avoid it.

### Deployment gotchas

- **User-data paths.** History, the debug log, pronunciations, and TTS output write to the per-OS data dir from `paths.data_dir()` (used by `app.py`, `flow.py` and `flow_studio.py`), not next to the code — safe under a read-only install dir. Bundled read-only resources (the HTML) still load from the install dir via `BASE_DIR`.
- **Ports 7500/7600 are still fixed**, but a collision is now handled: each entry point probes the port before binding and reports it cleanly — a console message for `app.py`/`flow.py`, a Windows dialog for `flow_studio.py` (which runs under `pythonw`, no console). Werkzeug swallows bind errors and `sys.exit`s inside its own thread, so the pre-bind probe is the reliable place to catch this. Auto-selecting a free port is a possible future improvement.
- **Ollama is separate.** It can't be bundled sanely — it's its own installer and background service. The dashboard's **set up cleanup** link installs it on Windows (only a validly Authenticode-signed installer is run); elsewhere, point users at ollama.com.

---

## Data & privacy

All processing is local. Files are written to `%LOCALAPPDATA%\FlowStudio\` (Windows), `~/Library/Application Support/FlowStudio/` (macOS) or `$XDG_DATA_HOME/flow-studio/`, default `~/.local/share/flow-studio/` (Linux):

- `settings.json` — dictation settings (hotkey, mic, Whisper model, cleanup model, language, overlay style).
- `flow_history.json` — your dictated text (last 200 entries). Plaintext.
- `flow_debug.log` — diagnostic log, appended to every run.
- `pronunciations.json` — your TTS pronunciation overrides.
- `outputs/` — generated TTS `.wav`/`.mp3` files (`outputs/_chunks/` is scratch, cleared on startup).

- `flow_studio.log` — console output of the installed (windowed) app.

- `update/` — a downloaded update, deleted after it's installed.

No telemetry. The default models ship in the installer. The network is used for the update check at launch (two small files from GitHub Releases; turn it off under Settings → General → *Check for updates*), an update you choose to install, models you choose to download, the optional Ollama setup, and URLs you paste into the TTS tool.
