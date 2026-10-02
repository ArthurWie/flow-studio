# Flow Studio

A desktop app with two tools that run on-device: speaking text aloud (TTS) and typing what the user says (Dictation).

## Language

### Tools

**TTS**:
The tool that reads text, a PDF or a web page aloud with Kokoro.
_Avoid_: reader, speaker, voice tool

**Job**:
One TTS generation, started by hand or from the PDF queue.
_Avoid_: task, request, render

**Dictation**:
The tool that records speech while the hotkey is active, transcribes it with Whisper and pastes the text into the app the user was in.
_Avoid_: flow, voice typing, transcription

**Gesture**:
How the user drives Dictation with the hotkey: **hold** (talk while held), **tap** (toggle on/off), **double-tap**.
_Avoid_: mode, trigger

**Overlay**:
The small window that shows Dictation is listening. It never takes focus from the app the user is typing into.
_Avoid_: popup, HUD, indicator

**Cleanup model**:
The optional local Ollama model that tidies a transcript before it is pasted.
_Avoid_: LLM, post-processor

### Platform

**OS adapter**:
The per-OS module that provides the hotkey, paste and foreground-app calls; the rest of the app never calls the OS directly.
_Avoid_: seam, backend, driver, platform layer

**Data dir**:
The per-user, per-OS folder holding everything the app writes: history, pronunciations, settings, downloaded models.
_Avoid_: app dir, config dir, install dir

**Settings**:
The user's choices (hotkey, mic, Whisper model, cleanup model, language, overlay style), kept in the data dir across restarts.
_Avoid_: config, preferences

**Compute policy**:
The rule for where and at what priority TTS and Whisper run, so foreground apps never lag.
_Avoid_: device config, performance mode

### Delivery

**Frozen app**:
The self-contained build of Flow Studio per OS; the user's machine needs no Python or package install.
_Avoid_: binary, bundle, env

**Bundled model**:
A default model shipped inside the frozen app; read-only and cannot be deleted.
_Avoid_: built-in model, preinstalled model

**Downloaded model**:
A model the user fetched through the model manager into the data dir; can be deleted.
_Avoid_: cached model, extra model

**Model manager**:
The in-app screen that lists, downloads and deletes models per tool.
_Avoid_: model store, model picker

**GPU pack**:
The optional download that swaps in a CUDA build of the frozen app on NVIDIA machines, falling back to CPU.
_Avoid_: CUDA pack, GPU mode, accelerator

**Selftest**:
The headless check the app runs on itself (`--selftest`): TTS speaks a fixed sentence, Whisper transcribes it, and the text must match.
_Avoid_: smoke test, health check
