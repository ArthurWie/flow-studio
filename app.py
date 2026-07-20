"""
Kokoro TTS Studio — a local web interface for the Kokoro-82M text-to-speech model.

Features:
  - Streaming playback (audio starts while later chunks still generate)
  - Word-by-word highlighting synced to the voice (click a word to jump there)
  - Voice blending with a balance slider
  - Long-form mode (text is chunked, generated with progress, stitched to one file)
  - Pronunciation dictionary (persisted to pronunciations.json)
  - MP3 export (requires ffmpeg on PATH; falls back gracefully if missing)

Run:  python app.py   → opens http://127.0.0.1:7500 in your browser.
Everything runs fully offline after the first model download.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path

import numpy as np
import soundfile as sf
from flask import Flask, jsonify, request, send_from_directory

SAMPLE_RATE = 24000
BASE_DIR = Path(__file__).resolve().parent
# Writable data lives outside the (possibly read-only) install dir. Twin of flow.py's DATA_DIR.
DATA_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "FlowStudio"
OUTPUT_DIR = DATA_DIR / "outputs"
CHUNK_DIR = OUTPUT_DIR / "_chunks"
PRON_FILE = DATA_DIR / "pronunciations.json"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
if CHUNK_DIR.exists():
    shutil.rmtree(CHUNK_DIR, ignore_errors=True)
CHUNK_DIR.mkdir(exist_ok=True)

VOICES = [
    ("af_heart", "Heart — US female"),
    ("af_bella", "Bella — US female"),
    ("af_nicole", "Nicole — US female, soft"),
    ("af_sarah", "Sarah — US female"),
    ("af_sky", "Sky — US female"),
    ("af_aoede", "Aoede — US female"),
    ("af_kore", "Kore — US female"),
    ("am_adam", "Adam — US male"),
    ("am_michael", "Michael — US male"),
    ("am_fenrir", "Fenrir — US male"),
    ("am_puck", "Puck — US male"),
    ("bf_emma", "Emma — UK female"),
    ("bf_isabella", "Isabella — UK female"),
    ("bm_george", "George — UK male"),
    ("bm_lewis", "Lewis — UK male"),
]

app = Flask(__name__)

_pipelines = {}
_pipeline_lock = threading.Lock()
_generate_lock = threading.Lock()
jobs = {}
MAX_JOBS = 5  # keep only recent jobs; older ones' chunk files are dead weight once superseded


def _evict_old_jobs():
    """Drop all but the most recent finished jobs and delete their streaming chunks.
    Called when a new job starts, so the current and previous results stay playable
    while memory (the jobs dict) and disk (CHUNK_DIR) stay bounded over a long session."""
    finished = [jid for jid in sorted(jobs, key=lambda j: jobs[j]["created"])
                if jobs[jid]["status"] != "running"]
    while len(jobs) > MAX_JOBS and finished:
        jid = finished.pop(0)
        for f in CHUNK_DIR.glob(f"{jid}_*.wav"):
            f.unlink(missing_ok=True)
        jobs.pop(jid, None)


def get_pipeline(lang_code):
    with _pipeline_lock:
        if lang_code not in _pipelines:
            from kokoro import KPipeline
            _pipelines[lang_code] = KPipeline(lang_code=lang_code)
        return _pipelines[lang_code]


def load_pronunciations():
    if PRON_FILE.exists():
        try:
            return json.loads(PRON_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_pronunciations(data):
    PRON_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def apply_pronunciations(text, prons):
    """Wrap dictionary words in Kokoro's [word](/phonemes/) override syntax."""
    for word, phonemes in prons.items():
        if not word or not phonemes:
            continue
        pattern = r"\b" + re.escape(word) + r"\b"
        text = re.sub(pattern, lambda m: f"[{m.group(0)}](/{phonemes}/)", text, flags=re.IGNORECASE)
    return text


def build_voice(pipeline, primary, secondary=None, balance=0.5):
    """Return a voice usable by the pipeline: a plain name, or a blended tensor."""
    if not secondary:
        return primary
    try:
        v1 = pipeline.load_voice(primary)
        v2 = pipeline.load_voice(secondary)
        blended = (1.0 - balance) * v1 + balance * v2
        return blended
    except Exception:
        # Fallback: kokoro averages comma-separated voices with equal weights.
        return f"{primary},{secondary}"


def fix_timestamps(words, offset, duration):
    """Fill in missing word timestamps by interpolating between known neighbours."""
    n = len(words)
    for i, w in enumerate(words):
        if w["start"] is None:
            prev_end = None
            for j in range(i - 1, -1, -1):
                if words[j]["end"] is not None:
                    prev_end = words[j]["end"]
                    break
            w["start"] = prev_end if prev_end is not None else offset
        if w["end"] is None:
            next_start = None
            for j in range(i + 1, n):
                if words[j]["start"] is not None:
                    next_start = words[j]["start"]
                    break
            w["end"] = next_start if next_start is not None else offset + duration
        if w["end"] < w["start"]:
            w["end"] = w["start"]
    return words


def run_job(job_id, text, primary, secondary, balance, speed):
    job = jobs[job_id]
    try:
        lang_code = "b" if primary.startswith("b") else "a"
        pipeline = get_pipeline(lang_code)
        voice = build_voice(pipeline, primary, secondary, balance)

        prons = load_pronunciations()
        prepared = apply_pronunciations(text, prons)

        # Paragraph map: a chunk starts a new paragraph if, in the ORIGINAL text,
        # a blank line precedes the position where this chunk's text begins.
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        para_starts = [para[:40].strip().lower() for para in paragraphs]

        def norm(s):
            return re.sub(r"\s+", " ", (s or "")).strip().lower()

        split_pattern = r"(?<=[.:;!?])\s+|\n+"
        offset = 0.0
        audio_parts = []
        para_cursor = 0  # index into para_starts we might match next

        with _generate_lock:
            for i, result in enumerate(pipeline(prepared, voice=voice, speed=speed,
                                                split_pattern=split_pattern)):
                audio = result.audio
                if hasattr(audio, "detach"):
                    audio = audio.detach().cpu().numpy()
                audio = np.asarray(audio, dtype=np.float32)
                duration = len(audio) / SAMPLE_RATE

                fname = f"{job_id}_{i}.wav"
                sf.write(CHUNK_DIR / fname, audio, SAMPLE_RATE)

                words = []
                tokens = getattr(result, "tokens", None) or []
                for t in tokens:
                    txt = getattr(t, "text", "") or ""
                    if not txt.strip():
                        continue
                    st = getattr(t, "start_ts", None)
                    et = getattr(t, "end_ts", None)
                    words.append({
                        "text": txt,
                        "ws": getattr(t, "whitespace", " ") or "",
                        "start": (offset + st) if st is not None else None,
                        "end": (offset + et) if et is not None else None,
                    })
                if words:
                    words = fix_timestamps(words, offset, duration)
                else:
                    words = [{"text": getattr(result, "graphemes", "…"), "ws": "",
                              "start": offset, "end": offset + duration}]

                # Does this chunk begin a paragraph from the original text?
                chunk_text = norm("".join(w["text"] + (" " if w.get("ws") else "")
                                          for w in words))
                para_break = False
                if para_cursor < len(para_starts) and chunk_text:
                    target = para_starts[para_cursor]
                    key = chunk_text[:12]
                    if key and (target.startswith(key) or chunk_text.startswith(target[:12])):
                        para_break = para_cursor > 0  # first paragraph isn't a "break"
                        para_cursor += 1

                audio_parts.append(audio)
                job["chunks"].append({
                    "url": f"/chunks/{fname}",
                    "offset": round(offset, 3),
                    "duration": round(duration, 3),
                    "para_break": para_break,
                    "words": words,
                })
                offset += duration

        if not audio_parts:
            raise RuntimeError("No audio was generated — is the text empty?")

        full = np.concatenate(audio_parts)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        final_name = f"tts_{stamp}.wav"
        sf.write(OUTPUT_DIR / final_name, full, SAMPLE_RATE)

        job["file"] = final_name
        job["total"] = round(offset, 3)
        job["status"] = "done"
    except Exception as exc:
        job["status"] = "error"
        job["error"] = str(exc)


@app.route("/")
def index():
    return PAGE_HTML


@app.route("/api/voices")
def api_voices():
    return jsonify([{"id": v, "label": l} for v, l in VOICES])


@app.route("/api/generate", methods=["POST"])
def api_generate():
    data = request.get_json(force=True)
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Enter some text first."}), 400
    primary = data.get("voice") or "af_heart"
    secondary = data.get("voice2") or None
    balance = float(data.get("balance") or 0.5)
    speed = float(data.get("speed") or 1.0)

    _evict_old_jobs()
    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = {"status": "running", "chunks": [], "file": None,
                    "total": None, "error": None, "created": time.time()}
    threading.Thread(target=run_job, args=(job_id, text, primary, secondary,
                                           balance, speed), daemon=True).start()
    return jsonify({"job": job_id})


@app.route("/api/job/<job_id>")
def api_job(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job."}), 404
    since = int(request.args.get("since", 0))
    return jsonify({
        "status": job["status"],
        "error": job["error"],
        "file": job["file"],
        "total": job["total"],
        "chunk_count": len(job["chunks"]),
        "chunks": job["chunks"][since:],
    })


@app.route("/api/export_mp3", methods=["POST"])
def api_export_mp3():
    data = request.get_json(force=True)
    name = os.path.basename(data.get("file") or "")
    wav_path = OUTPUT_DIR / name
    if not name.endswith(".wav") or not wav_path.exists():
        return jsonify({"error": "File not found."}), 404
    if not shutil.which("ffmpeg"):
        return jsonify({"error": "ffmpeg is not installed or not on PATH. "
                                 "Install it from ffmpeg.org to enable MP3 export."}), 400
    mp3_name = name[:-4] + ".mp3"
    mp3_path = OUTPUT_DIR / mp3_name
    try:
        subprocess.run(["ffmpeg", "-y", "-i", str(wav_path), "-b:a", "160k",
                        str(mp3_path)], check=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        return jsonify({"error": "ffmpeg failed: " + exc.stderr.decode(errors="ignore")[-300:]}), 500
    return jsonify({"file": mp3_name})


@app.route("/api/pronunciations", methods=["GET", "POST", "DELETE"])
def api_pronunciations():
    prons = load_pronunciations()
    if request.method == "GET":
        return jsonify(prons)
    data = request.get_json(force=True)
    word = (data.get("word") or "").strip()
    if request.method == "POST":
        phonemes = (data.get("phonemes") or "").strip().strip("/")
        if not word or not phonemes:
            return jsonify({"error": "Both word and phonemes are needed."}), 400
        prons[word] = phonemes
    else:
        prons.pop(word, None)
    save_pronunciations(prons)
    return jsonify(prons)


@app.route("/chunks/<path:name>")
def serve_chunk(name):
    return send_from_directory(CHUNK_DIR, name)


@app.route("/outputs/<path:name>")
def serve_output(name):
    return send_from_directory(OUTPUT_DIR, name, as_attachment=True)


PAGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kokoro TTS Studio</title>
<style>
  :root {
    --bg: #ffffff; --panel: #fafafa; --border: #e5e7eb; --border-2: #d1d5db;
    --text: #111827; --muted: #6b7280; --faint: #9ca3af;
    --black: #111827; --danger: #dc2626; --radius: 10px;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font-family: system-ui, "Segoe UI", -apple-system, sans-serif;
    display: flex; flex-direction: column; overflow: hidden;
  }
  header {
    display: flex; align-items: center; gap: 10px; padding: 12px 24px;
    border-bottom: 1px solid var(--border); flex: none;
  }
  header h1 { font-size: 15px; font-weight: 600; margin: 0; }
  header span { color: var(--faint); font-size: 12px; }
  main { flex: 1; display: flex; min-height: 0; }
  .editor-col { flex: 1; display: flex; flex-direction: column; min-width: 0; position: relative; }
  .editor-head {
    display: flex; align-items: center; gap: 10px; padding: 10px 40px 0; flex: none;
  }
  .chip {
    display: inline-flex; align-items: center; gap: 6px; font-size: 13px;
    border: 1px solid var(--border); border-radius: 999px; padding: 4px 12px;
    color: var(--text); background: var(--bg);
  }
  .chip b { font-weight: 600; }
  .chip .swatch { width: 8px; height: 8px; border-radius: 50%; background: #111827; }
  #editBtn { display: none; margin-left: auto; }
  textarea#text {
    flex: 1; width: 100%; border: none; resize: none; outline: none;
    padding: 20px 40px 30px; font: inherit; font-size: 17px; line-height: 1.9;
    color: var(--text); background: var(--bg);
  }
  textarea#text::placeholder { color: var(--faint); }
  #reader {
    flex: 1; display: none; overflow-y: auto; padding: 20px 40px 40px;
    font-size: 17px; line-height: 1.95;
  }
  #reader .para { margin: 0 0 1.15em; max-width: 820px; }
  #reader .para:last-child { margin-bottom: 0; }
  #reader span.w { cursor: pointer; border-radius: 4px; padding: 1px 3px; }
  #reader span.w:hover { background: var(--panel); }
  #reader span.w.spoken { color: var(--faint); }
  #reader span.w.now { background: var(--black); color: #ffffff; }
  aside {
    width: 320px; flex: none; border-left: 1px solid var(--border);
    overflow-y: auto; padding: 20px; background: var(--bg);
  }
  aside h2 {
    font-size: 11px; font-weight: 600; letter-spacing: 1.2px; text-transform: uppercase;
    color: var(--faint); margin: 0 0 12px;
  }
  aside section { margin-bottom: 28px; }
  label { font-size: 13px; color: var(--muted); display: block; margin: 0 0 5px; }
  select, input[type=text] {
    width: 100%; background: var(--bg); color: var(--text);
    border: 1px solid var(--border-2); border-radius: 8px;
    padding: 8px 10px; font: inherit; font-size: 14px;
  }
  select:focus, input[type=text]:focus { outline: 2px solid var(--black); outline-offset: -1px; }
  .field { margin-bottom: 14px; }
  input[type=range] { width: 100%; accent-color: var(--black); }
  .val { color: var(--text); font-weight: 500; font-variant-numeric: tabular-nums; }
  .blend-toggle { display: flex; align-items: center; gap: 8px; font-size: 13px;
    color: var(--muted); cursor: pointer; user-select: none; margin-bottom: 10px; }
  .blend-box { display: none; }
  .blend-box.open { display: block; }
  button {
    font: inherit; cursor: pointer; border-radius: 8px; font-size: 14px;
    border: 1px solid var(--border-2); background: var(--bg); color: var(--text);
    padding: 8px 14px;
  }
  button:hover { background: var(--panel); }
  button.primary {
    background: var(--black); color: #fff; border-color: var(--black);
    font-weight: 600; padding: 10px 22px; border-radius: 999px;
  }
  button.primary:hover { background: #000; }
  button.primary:disabled { background: var(--border-2); border-color: var(--border-2); cursor: default; }
  button.small { font-size: 13px; padding: 5px 12px; }
  .hist-item { padding: 10px 0; border-bottom: 1px solid var(--border); font-size: 13px; }
  .hist-item .txt { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .hist-item .meta { display: flex; gap: 10px; color: var(--faint); font-size: 12px;
    margin-top: 3px; align-items: center; }
  .hist-item a { color: var(--text); margin-left: auto; text-decoration: underline; }
  .pron-row { display: flex; gap: 6px; margin-bottom: 8px; }
  .pron-row input { min-width: 0; }
  .pron-list .p-item { display: flex; gap: 8px; align-items: center; padding: 6px 0;
    border-bottom: 1px solid var(--border); font-size: 13px; }
  .pron-list .p-item code { color: var(--muted); font-size: 12px; }
  .pron-list .p-item button { margin-left: auto; }
  .hint { font-size: 12px; color: var(--faint); margin-top: 8px; line-height: 1.5; }
  footer {
    flex: none; border-top: 1px solid var(--border); padding: 10px 24px;
    display: flex; align-items: center; gap: 14px; background: var(--bg);
  }
  footer .status { font-size: 13px; color: var(--muted); min-width: 0;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: 1; }
  footer .status .err { color: var(--danger); }
  #play {
    width: 40px; height: 40px; border-radius: 50%; flex: none; font-size: 15px;
    padding: 0; background: var(--black); color: #fff; border-color: var(--black);
  }
  #play:disabled { background: var(--border-2); border-color: var(--border-2); }
  .bar { width: 220px; height: 5px; background: var(--border); border-radius: 3px;
    cursor: pointer; flex: none; }
  .bar .fill { height: 100%; width: 0%; background: var(--black); border-radius: 3px; }
  .time { font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums;
    white-space: nowrap; flex: none; }
  .chars { font-size: 12px; color: var(--faint); white-space: nowrap; flex: none; }
  #fileActions { display: none; gap: 8px; flex: none; }
  #fileActions a { text-decoration: none; }
  @media (max-width: 900px) {
    aside { display: none; }
    .bar { width: 120px; }
  }
</style>
</head>
<body>
<header>
  <h1>Kokoro TTS Studio</h1>
  <span>local · offline · saves to outputs/</span>
</header>

<main>
  <div class="editor-col">
    <div class="editor-head">
      <span class="chip"><span class="swatch"></span><b id="chipVoice">Heart</b><span id="chipExtra"></span></span>
      <button class="small" id="editBtn">Edit text</button>
    </div>
    <textarea id="text" placeholder="Start typing or paste anything here — a sentence or a whole chapter. Long texts stream: playback begins while the rest is still generating, and each word lights up as it's spoken."></textarea>
    <div id="reader"></div>
  </div>

  <aside>
    <section>
      <h2>Settings</h2>
      <div class="field">
        <label for="voice">Voice</label>
        <select id="voice"></select>
      </div>
      <div class="field">
        <label for="speed">Speed <span class="val" id="speedVal">1.0×</span></label>
        <input type="range" id="speed" min="0.5" max="2" step="0.1" value="1">
      </div>
      <label class="blend-toggle"><input type="checkbox" id="blendOn"> Blend with a second voice</label>
      <div class="blend-box" id="blendBox">
        <div class="field">
          <label for="voice2">Second voice</label>
          <select id="voice2"></select>
        </div>
        <div class="field">
          <label>Balance <span class="val" id="balVal">50 / 50</span></label>
          <input type="range" id="balance" min="0" max="100" step="5" value="50">
        </div>
      </div>
    </section>
    <section>
      <h2>History</h2>
      <div id="history"><span class="hint">Nothing yet this session.</span></div>
    </section>
    <section>
      <h2>Pronunciation</h2>
      <div class="pron-row">
        <input type="text" id="pWord" placeholder="Word">
        <input type="text" id="pPhon" placeholder="Phonemes">
        <button class="small" id="pAdd">Add</button>
      </div>
      <div class="hint">Fixes apply to every future generation. Phonemes use Kokoro's notation, e.g. kˈOkəɹO — ask Claude for the phonemes of any word.</div>
      <div class="pron-list" id="pronList"></div>
    </section>
  </aside>
</main>

<footer>
  <button id="play" disabled>▶</button>
  <div class="bar" id="bar"><div class="fill" id="fill"></div></div>
  <span class="time" id="time">0:00 / 0:00</span>
  <span class="status" id="status"></span>
  <div id="fileActions">
    <a id="dlWav" href="#"><button class="small">Download WAV</button></a>
    <button class="small" id="mp3Btn">Export MP3</button>
    <a id="dlMp3" href="#" style="display:none"><button class="small">Download MP3</button></a>
  </div>
  <span class="chars" id="chars">0 characters</span>
  <button class="primary" id="go">Generate speech</button>
</footer>

<script>
const $ = id => document.getElementById(id);
let chunks = [], curIdx = -1, totalDur = 0, jobDone = false, jobId = null, poller = null;
let wordEls = [], lastNow = -1;
const audio = new Audio();

function fmt(t) {
  t = Math.max(0, t || 0);
  const m = Math.floor(t / 60), s = Math.floor(t % 60);
  return m + ":" + String(s).padStart(2, "0");
}

async function loadVoices() {
  const vs = await (await fetch("/api/voices")).json();
  for (const sel of [$("voice"), $("voice2")]) {
    sel.innerHTML = vs.map(v => `<option value="${v.id}">${v.label}</option>`).join("");
  }
  $("voice2").value = "af_nicole";
  updateChip();
}

function updateChip() {
  $("chipVoice").textContent = $("voice").value;
  $("chipExtra").textContent = $("blendOn").checked
    ? " + " + $("voice2").value + " · " + (+$("speed").value).toFixed(1) + "\u00d7"
    : " · " + (+$("speed").value).toFixed(1) + "\u00d7";
}

$("voice").onchange = updateChip;
$("voice2").onchange = updateChip;
$("speed").oninput = () => { $("speedVal").textContent = (+$("speed").value).toFixed(1) + "\u00d7"; updateChip(); };
$("balance").oninput = () => {
  const b = +$("balance").value;
  $("balVal").textContent = (100 - b) + " / " + b;
};
$("blendOn").onchange = () => { $("blendBox").classList.toggle("open", $("blendOn").checked); updateChip(); };
$("text").oninput = () => $("chars").textContent = $("text").value.length.toLocaleString() + " characters";

function showReader(on) {
  $("reader").style.display = on ? "block" : "none";
  $("text").style.display = on ? "none" : "block";
  $("editBtn").style.display = on ? "inline-block" : "none";
}
$("editBtn").onclick = () => { audio.pause(); $("play").textContent = "\u25b6"; showReader(false); };

function globalTime() {
  if (curIdx < 0 || !chunks[curIdx]) return 0;
  return chunks[curIdx].offset + audio.currentTime;
}

let curPara = null, firstInPara = true;

function newParagraph() {
  curPara = document.createElement("p");
  curPara.className = "para";
  $("reader").appendChild(curPara);
  firstInPara = true;
}

function isPunct(t) {
  // Leading punctuation that should hug the previous word (no space before).
  return /^[.,;:!?\u2026\u2019'")\]\u201d\u2013\u2014%]/.test(t.trim());
}

function renderChunk(chunk) {
  if (!curPara) newParagraph();
  else if (chunk.para_break) newParagraph();
  for (const w of chunk.words) {
    const txt = (w.text || "");
    const span = document.createElement("span");
    span.className = "w";
    span.textContent = txt.trim();
    span.dataset.start = w.start;
    span.dataset.end = w.end;
    span.onclick = () => seekTo(w.start + 0.001);

    if (!firstInPara && !isPunct(txt)) {
      curPara.appendChild(document.createTextNode(" "));
    }
    curPara.appendChild(span);
    wordEls.push(span);
    firstInPara = false;
  }
}

function highlight() {
  const t = globalTime();
  let nowEl = null;
  for (const el of wordEls) {
    const s = +el.dataset.start, e = +el.dataset.end;
    if (t >= e) { el.classList.add("spoken"); el.classList.remove("now"); }
    else if (t >= s) { el.classList.add("now"); el.classList.remove("spoken"); nowEl = el; }
    else { el.classList.remove("now", "spoken"); }
  }
  if (nowEl && wordEls.indexOf(nowEl) !== lastNow) {
    lastNow = wordEls.indexOf(nowEl);
    nowEl.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
  const denom = jobDone && totalDur ? totalDur : (chunks.length ? chunks[chunks.length-1].offset + chunks[chunks.length-1].duration : 0);
  $("fill").style.width = denom ? Math.min(100, t / denom * 100) + "%" : "0%";
  $("time").textContent = fmt(t) + " / " + fmt(denom);
}

function playChunk(i, at = 0) {
  if (i >= chunks.length) { curIdx = chunks.length; return; }
  curIdx = i;
  audio.src = chunks[i].url;
  audio.currentTime = at;
  audio.play().then(() => { $("play").textContent = "\u23f8"; }).catch(() => {});
}

function seekTo(t) {
  for (let i = 0; i < chunks.length; i++) {
    const c = chunks[i];
    if (t >= c.offset && t < c.offset + c.duration) {
      if (i === curIdx) { audio.currentTime = t - c.offset; audio.play(); $("play").textContent = "\u23f8"; }
      else playChunk(i, t - c.offset);
      return;
    }
  }
}

audio.addEventListener("timeupdate", highlight);
audio.addEventListener("ended", () => {
  if (curIdx + 1 < chunks.length) playChunk(curIdx + 1);
  else if (!jobDone) waitForNext();
  else { $("play").textContent = "\u25b6"; highlight(); }
});

function waitForNext() {
  const want = curIdx + 1;
  const check = setInterval(() => {
    if (chunks.length > want) { clearInterval(check); playChunk(want); }
    else if (jobDone) { clearInterval(check); $("play").textContent = "\u25b6"; }
  }, 250);
}

$("play").onclick = () => {
  if (audio.paused) {
    if (curIdx < 0 || curIdx >= chunks.length) playChunk(0);
    else audio.play().then(() => $("play").textContent = "\u23f8");
  } else { audio.pause(); $("play").textContent = "\u25b6"; }
};

$("bar").onclick = (e) => {
  const denom = jobDone && totalDur ? totalDur : (chunks.length ? chunks[chunks.length-1].offset + chunks[chunks.length-1].duration : 0);
  if (!denom) return;
  const rect = $("bar").getBoundingClientRect();
  seekTo((e.clientX - rect.left) / rect.width * denom);
};

$("go").onclick = async () => {
  const text = $("text").value.trim();
  if (!text) { $("status").innerHTML = '<span class="err">Enter some text first.</span>'; return; }
  audio.pause();
  chunks = []; curIdx = -1; totalDur = 0; jobDone = false; wordEls = []; lastNow = -1;
  curPara = null; firstInPara = true;
  $("reader").innerHTML = ""; $("fileActions").style.display = "none";
  $("dlMp3").style.display = "none"; $("play").disabled = true;
  $("go").disabled = true;
  showReader(true);
  $("status").textContent = "Starting… (first run downloads the model, ~330 MB)";

  const body = {
    text, voice: $("voice").value, speed: +$("speed").value,
    voice2: $("blendOn").checked ? $("voice2").value : null,
    balance: +$("balance").value / 100,
  };
  const res = await fetch("/api/generate", { method: "POST",
    headers: {"Content-Type": "application/json"}, body: JSON.stringify(body) });
  const data = await res.json();
  if (data.error) { $("status").innerHTML = '<span class="err">' + data.error + "</span>"; $("go").disabled = false; showReader(false); return; }
  jobId = data.job;
  poll();
};

function poll() {
  clearInterval(poller);
  poller = setInterval(async () => {
    const res = await fetch(`/api/job/${jobId}?since=${chunks.length}`);
    const data = await res.json();
    if (data.error) { fail(data.error); return; }
    for (const c of data.chunks) { chunks.push(c); renderChunk(c); }
    if (chunks.length && $("play").disabled) { $("play").disabled = false; playChunk(0); }
    if (data.status === "running") {
      $("status").textContent = "Generating… " + chunks.length + " chunk" + (chunks.length === 1 ? "" : "s") + " ready, playback streaming.";
    } else if (data.status === "error") {
      fail(data.error);
    } else if (data.status === "done") {
      clearInterval(poller);
      jobDone = true; totalDur = data.total;
      $("status").textContent = "Done — saved as outputs/" + data.file + " (" + fmt(data.total) + ")";
      $("go").disabled = false;
      $("fileActions").style.display = "flex";
      $("dlWav").href = "/outputs/" + data.file;
      $("mp3Btn").dataset.file = data.file;
      addHistory($("text").value, data.file, data.total);
    }
  }, 600);
}

function fail(msg) {
  clearInterval(poller);
  $("status").innerHTML = '<span class="err">' + msg + "</span>";
  $("go").disabled = false;
}

$("mp3Btn").onclick = async () => {
  $("mp3Btn").disabled = true; $("mp3Btn").textContent = "Converting…";
  const res = await fetch("/api/export_mp3", { method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ file: $("mp3Btn").dataset.file }) });
  const data = await res.json();
  $("mp3Btn").disabled = false; $("mp3Btn").textContent = "Export MP3";
  if (data.error) { $("status").innerHTML = '<span class="err">' + data.error + "</span>"; return; }
  $("dlMp3").href = "/outputs/" + data.file;
  $("dlMp3").style.display = "inline-block";
};

function addHistory(text, file, dur) {
  const box = $("history");
  if (box.querySelector(".hint")) box.innerHTML = "";
  const div = document.createElement("div");
  div.className = "hist-item";
  const short = text.length > 70 ? text.slice(0, 70) + "…" : text;
  div.innerHTML = `<div class="txt"></div>
    <div class="meta"><span>${$("voice").value}${$("blendOn").checked ? " + " + $("voice2").value : ""}</span>
    <span>${fmt(dur)}</span><a href="/outputs/${file}">download</a></div>`;
  div.querySelector(".txt").textContent = short;
  box.prepend(div);
}

async function loadProns() {
  const prons = await (await fetch("/api/pronunciations")).json();
  const box = $("pronList");
  box.innerHTML = "";
  for (const [w, p] of Object.entries(prons)) {
    const div = document.createElement("div");
    div.className = "p-item";
    div.innerHTML = `<span></span><code>/${p}/</code><button class="small">Remove</button>`;
    div.querySelector("span").textContent = w;
    div.querySelector("button").onclick = async () => {
      await fetch("/api/pronunciations", { method: "DELETE",
        headers: {"Content-Type": "application/json"}, body: JSON.stringify({ word: w }) });
      loadProns();
    };
    box.appendChild(div);
  }
}

$("pAdd").onclick = async () => {
  const word = $("pWord").value.trim(), phonemes = $("pPhon").value.trim();
  if (!word || !phonemes) return;
  await fetch("/api/pronunciations", { method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ word, phonemes }) });
  $("pWord").value = ""; $("pPhon").value = "";
  loadProns();
};

loadVoices();
loadProns();
</script>
</body>
</html>"""


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # Windows console defaults to cp1252
    except Exception:
        pass
    import socket
    with socket.socket() as _s:  # Werkzeug swallows bind errors and sys.exits itself, so probe first
        if _s.connect_ex(("127.0.0.1", 7500)) == 0:
            raise SystemExit("\n  [!] Port 7500 is already in use - is Kokoro TTS Studio "
                             "already running?\n      Close the other instance and try again.\n")
    url = "http://127.0.0.1:7500"
    print(f"\n  Kokoro TTS Studio → {url}\n  Files are saved to: {OUTPUT_DIR}\n")
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=7500, debug=False)