# Flow Studio — Lightweight Installer & Download-on-Setup

**Date:** 2026-07-20
**Status:** Approved design, ready for implementation planning
**Topic:** A small Windows installer that assembles the heavy runtime + models on the user's machine at first run, with a guided setup UI.

---

## 1. Context & goal

Flow Studio is a local, Windows-only ML desktop app bundling two tools — Kokoro TTS (`app.py`, port 7500) and Flow dictation (`flow.py`, port 7600), shown in one WebView2 window by `flow_studio.py`. Everything runs offline after first-run downloads.

The full dependency set is ~1.1 GB (torch alone is 470 MB) plus ~575 MB of ML models. A PyInstaller spike confirmed the whole stack bundles and runs, producing a ~1.1 GB `onedir` build. That build is retained as an offline/air-gapped alternative but is **not** the primary distribution path.

**Goal:** ship a *small* installer (tens of MB) that, on first run, downloads and assembles the engine and models on the user's machine — offloading the heavy bytes to third-party infrastructure (PyPI, Hugging Face, Ollama) so the project needs near-zero hosting (a free GitHub Releases page for the small installer).

**Explicit trade-off accepted:** this leans toward *low infrastructure/bandwidth* over *robustness*. Assembling a Python environment on an unknown client depends on the user's network and on PyPI/HF/Ollama uptime, and is the more fragile of the two accepted patterns (the other being "bundle everything"). This is the de-facto standard for local-ML desktop apps (AUTOMATIC1111, ComfyUI, InvokeAI, oobabooga, Pinokio).

## 2. Requirements & decisions

Derived from the design dialogue:

- **First-run experience:** an **auto guided setup screen**. The user approves each step, but every download is automatic — they never visit an external website or run manual install steps elsewhere. Progress is visible per step.
- **Cleanup model (Ollama + qwen):** an **optional** step, but auto-handled when chosen — setup downloads Ollama's official installer from Ollama's servers, runs it, and pulls `qwen2.5:3b`. Dictation still works without it (types the raw transcript). Never blocks setup.
  - *Caveat:* the Ollama installer is third-party software; Windows may show its own installer/permission prompt for that one step (Ollama installs per-user, so likely no admin/UAC). This cannot be fully suppressed.
- **Infrastructure:** offload the heavy lifting to third parties — engine from **PyPI**, models from **Hugging Face**, Ollama from **Ollama's servers**. The project hosts only the small installer (GitHub Releases).
- **Chosen approach: small bootstrapper + `uv`** (see §3). `uv` assembles the Python environment and installs dependencies; a tiny stdlib-only `bootstrap.exe` shows the setup UI and orchestrates.
  - *Rejected — embeddable Python + hand-rolled pip:* smallest installer, but embeddable Python + pip is finicky (path config, no venv/tkinter) and more fragile.
  - *Rejected — Inno Setup downloads everything at install time:* conflates install-time with a first-run task, ~1.5 GB pulled during "install," weak progress/resume UX.

## 3. Architecture & what ships

**Installer:** Inno Setup, ~40–50 MB, **per-user (no admin)**, installs to `%LOCALAPPDATA%\Programs\FlowStudio\`:

```
Programs\FlowStudio\
  flow_studio.py, app.py, flow.py, flow.ico   ← the app (source, unchanged)
  requirements.txt                            ← the engine manifest (pinned)
  bootstrap.exe                               ← tiny stdlib-only launcher (PyInstaller)
  uv.exe                                      ← bundled; assembles Python + deps
  env\                                        ← the assembled venv (created at first run)
```

**Two install locations, on purpose:**
- **Program files** → `%LOCALAPPDATA%\Programs\FlowStudio\` (code, `uv`, and the `env\` venv). Removed on uninstall.
- **User data** → `%LOCALAPPDATA%\FlowStudio\` (the existing `DATA_DIR`: history, outputs, pronunciations, logs). Survives uninstall/reinstall.

**`bootstrap.exe` is the gatekeeper.** The Start-menu/desktop shortcut points at it, never the app directly. On every launch it:
1. Checks *"is the environment ready?"* via a marker file `env\.setup_complete` — a small JSON file recording the `requirements.txt` hash and a `models_complete` boolean. Ready = `env\` exists + recorded hash matches current `requirements.txt` + `models_complete` is true.
2. **Ready** → launches the real app (`env\Scripts\pythonw.exe flow_studio.py`) and gets out of the way.
3. **Not ready** (fresh install, or a later version bumped `requirements.txt`) → runs guided setup, then launches the app.

Because `bootstrap.exe` is stdlib-only (no torch), it is always runnable and can show a setup UI *before* the engine exists. The 1.1 GB never ships; `uv` builds it on the machine.

## 4. Setup flow

**Detection:** as above — marker file `env\.setup_complete` (JSON: `requirements.txt` hash + `models_complete` flag). A future `requirements.txt` bump auto-invalidates the marker, re-running only the engine step.

**Serving the UI:** `bootstrap.exe` starts Python's stdlib `http.server` on a free local port, serves one inline setup page styled like Flow (cream/ink/orange), and opens the browser to it. The page talks to the bootstrapper over small JSON endpoints, reusing the worker-thread + status-polling pattern the app already uses for TTS jobs.

**Guided steps** (each approved by a click; every download automatic):

1. **Welcome / precheck** — shows what installs + total (~1.7 GB), checks free disk (~3–4 GB headroom) + reachability of PyPI and HF. One "Install" button. If offline, tells the user setup needs internet once and does not start.
2. **Engine (~1.1 GB)** — `uv venv env` → `uv pip install -r requirements.txt` from PyPI. Progress from `uv` output.
3. **Voices + speech (~575 MB)** — runs the app's *own* code in the new env (`app.get_pipeline("a")`, `app.get_pipeline("b")`, `flow.get_whisper("small")`) to trigger the exact HF downloads the app needs. Reusing real code paths avoids guessing repo IDs or drifting from what the app loads.
4. **Cleanup (optional)** — probes for Ollama. Present → offer `ollama pull qwen2.5:3b`. Absent → "Enable cleanup?" downloads Ollama's official installer, runs it, then pulls. Fully skippable.
5. **Done** — writes the marker, launches `env\Scripts\pythonw.exe flow_studio.py`; the app's native window opens; the setup tab shows "You're all set."

**Model location (design choice):** set `HF_HOME` to `%LOCALAPPDATA%\FlowStudio\models` so models live in a known, self-contained location (not the default `~/.cache`). The bootstrapper both pre-warms *and* launches the app with the same `HF_HOME`, so the app finds the already-fetched models instead of re-downloading. v1 pre-fetches Whisper **small** only; larger sizes download on demand in-app.

## 5. Error handling, resume & edge cases

**Resume is the backbone** — every step is idempotent and the success marker is written only at the very end:
- `uv venv` no-ops if the env exists; `uv pip install -r requirements.txt` reinstalls only what's missing.
- HF downloads resume partial files and skip complete ones.
- If setup is killed/crashes/quits mid-way, relaunching `bootstrap.exe` re-runs setup and picks up where it left off — no bespoke resume state machine, just idempotent steps + one marker.

**Per-step errors** (each step: pending / running / done / error):
- PyPI or HF unreachable → error + **Retry** button that re-runs just that step.
- `uv`/pip failure → tail of output shown on the page; full log to `%LOCALAPPDATA%\FlowStudio\setup.log`; "Copy log" button.
- Disk fills mid-install → surfaced by the failing step with a free-space message.
- Ollama step fails → never blocks; marks cleanup "not set up," notes dictation will use raw transcripts, setup still completes.

**Edge cases:**
- Setup-UI port in use → pick a free port (connect-probe, as in `flow_studio.py`'s port handling).
- Browser tab closed mid-setup → the worker keeps running in `bootstrap.exe`; reopening reconnects to live status.
- Double-launch → single-instance guard (same pattern as `flow_studio.py`) focuses the existing setup instead of starting a second.
- Hard-killed, wedged env → a "Reset & reinstall engine" escape hatch (delete `env\`, redo).
- Defender/AV flagging `uv` or the Ollama installer → documented known risk; code-signing later mitigates.

**Philosophy:** the app is never launched until its env + models are verified present. Either the marker is set (fully ready) or setup transparently re-runs. No half-working state that looks fine but isn't.

## 6. Build pipeline & testing

**Producing the installer** (a new `package.ps1`):
1. Build `bootstrap.exe` from `bootstrap.py` with PyInstaller — stdlib-only, ~10 MB, builds in seconds.
2. Download a pinned `uv.exe` from Astral's releases.
3. Stage: app source + `requirements.txt` + `bootstrap.exe` + `uv.exe`.
4. Run Inno Setup (`FlowStudio.iss`) → `FlowStudioSetup.exe` (~40–50 MB): per-user install, Start-menu + desktop shortcut → `bootstrap.exe`, uninstaller.

The existing `build.ps1` (full 1.1 GB bundle) is retained as the documented offline/air-gapped alternative.

**Testing** (scaled to what's testable):
- **Bootstrap logic** — fast assert-based tests (same style as the repo's `--selftest`s), mocking subprocess/network: ready-vs-setup detection, marker hash match/mismatch, step state transitions, resume (marker only on success), free-port selection, precheck.
- **End-to-end** — a `bootstrap.exe --selftest` that runs the real uv install + model fetch into a throwaway temp env and asserts the app's own `flow_studio.py --selftest` passes. Run manually (minutes + GBs), not in a fast loop.
- **Acceptance** — a clean-VM install before any release. For an installer, that is the only test that truly counts.

## 7. v1 scope — deliberately cut (YAGNI)

- **CPU-only torch** (matches the app: Whisper `device="cpu"`, Kokoro on CPU). No GPU/CUDA detection.
- Pre-fetch **Whisper small** only; larger sizes download on demand in-app.
- **Install once** — no in-app auto-updater. The marker enables a future version to re-run the engine on a `requirements.txt` bump; delivering new app versions = reinstall for v1.
- No air-gapped installer (that is the `build.ps1` bundle, kept as the alternative).
- No code-signing in the v1 build itself — documented as the step before any public release.

## 8. New files this introduces

- `bootstrap.py` — the bootstrapper: stdlib `http.server` + inline setup page + `uv` orchestration + gatekeeper/launch logic.
- `FlowStudio.iss` — Inno Setup script.
- `package.ps1` — the installer build pipeline.
- README — install-section rewrite to reflect the real end-user install.

## 9. Out of scope / future

**Platform scope: Windows-only.** This installer and the Flow Studio app target Windows exclusively. macOS/Linux are out of scope. The split is clean but not portable as-is:
- **`app.py` (Kokoro TTS) is already cross-platform** — pure Python (Flask, numpy, soundfile, kokoro/torch), no OS-specific calls.
- **`flow.py` (dictation) and `flow_studio.py` (shell) are hard-locked to Windows** — they use `ctypes.windll` for the global hotkey (Win32 `RegisterHotKey`), clipboard (`OpenClipboard`/`SetClipboardData`), focused-app paste (`GetForegroundWindow`/`SetForegroundWindow`), and the always-on-top overlay. `ctypes.windll` does not exist on macOS, so these modules will not even import there.
- **This installer is Windows-by-construction** — Inno Setup, `%LOCALAPPDATA%`, `.exe`, WebView2.

A future Mac effort would be two separate pieces of very different cost: "Kokoro TTS on Mac" is cheap (the code is already portable; needs a `.app`/`.dmg` packaging path), while "Flow dictation on Mac" is a genuine port — reimplementing the hotkey/clipboard/paste/overlay against macOS APIs (`CGEventTap`, `NSPasteboard`, Accessibility), plus the macOS realities of TCC permission prompts (Accessibility + Input Monitoring) and code-signing/notarization for distribution. Treat it as a later milestone, TTS-first.

- GPU/CUDA torch variant and detection.
- In-app auto-update / delta updates.
- Code-signing the installer + `bootstrap.exe` (required before public distribution to avoid SmartScreen).
- Air-gapped/offline installer as a first-class option (the bundle exists but is not packaged for distribution).
- Non-English model pre-fetch.
