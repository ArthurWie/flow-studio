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
import queue
import threading
import time
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path

import numpy as np
import soundfile as sf
from flask import Flask, Response, jsonify, request, send_from_directory

SAMPLE_RATE = 24000
BASE_DIR = Path(__file__).resolve().parent
# Writable data lives outside the (possibly read-only) install dir. Twin of flow.py's DATA_DIR.
DATA_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "FlowStudio"
OUTPUT_DIR = DATA_DIR / "outputs"
CHUNK_DIR = OUTPUT_DIR / "_chunks"
PDF_DIR = DATA_DIR / "_pdfs"
PRON_FILE = DATA_DIR / "pronunciations.json"

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
queue_items = {}      # id -> a PDF waiting to be read, or already read
queue_order = []      # ids, oldest first, as shown in the panel
work_queue = queue.Queue()
MAX_JOBS = 5  # keep only recent jobs; older ones' chunk files are dead weight once superseded


def _evict_old_jobs():
    """Drop all but the most recent finished jobs and delete their streaming chunks.
    Called when a new job starts, so the current and previous results stay playable
    while memory (the jobs dict) and disk (CHUNK_DIR) stay bounded over a long session."""
    keep = {it["job"] for it in queue_items.values() if it.get("job")}
    finished = [jid for jid in sorted(jobs, key=lambda j: jobs[j]["created"])
                if jobs[jid]["status"] != "running" and jid not in keep]
    while len(jobs) > MAX_JOBS and finished:
        jid = finished.pop(0)
        for f in CHUNK_DIR.glob(f"{jid}_*.wav"):
            f.unlink(missing_ok=True)
        jobs.pop(jid, None)


def new_job():
    """Register a fresh running job and return its id."""
    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = {"status": "running", "chunks": [], "file": None,
                    "total": None, "error": None, "created": time.time()}
    return job_id


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
    job["chars_total"] = len(text)   # denominator for the queue's progress bar
    job["chars_done"] = 0
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
                if job.get("cancel"):
                    break
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
                job["chars_done"] += sum(len(w["text"]) + len(w.get("ws") or "")
                                         for w in words)
                job["chunks"].append({
                    "url": f"/chunks/{fname}",
                    "offset": round(offset, 3),
                    "duration": round(duration, 3),
                    "para_break": para_break,
                    "words": words,
                })
                offset += duration

        if job.get("cancel"):
            job["status"] = "cancelled"
            return
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

    # Kokoro generates under one global lock, so a manual run starts only once the
    # queue's current document is finished. Say so rather than sitting on "Starting...".
    behind = next((it["name"] for it in queue_items.values()
                   if it["status"] == "running"), None)

    _evict_old_jobs()
    job_id = new_job()
    threading.Thread(target=run_job, args=(job_id, text, primary, secondary,
                                           balance, speed), daemon=True).start()
    return jsonify({"job": job_id, "behind": behind})


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


@app.route("/api/job/<job_id>/stop", methods=["POST"])
def api_job_stop(job_id):
    """run_job checks this flag between chunks, so it stops at the next one."""
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job."}), 404
    if job["status"] == "running":
        job["cancel"] = True
    return jsonify({"status": job["status"]})


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


# Dot leaders and bare bullets are layout, not speech: a contents line reading
# "Results . . . . . 4" should be spoken as "Results 4". Dropping them at the source
# keeps the text and the boxes in step.
_LEADER_RE = re.compile(r"^[.·•․‥…‧⁃_]+$")


def _is_spoken(word):
    w = (word or "").strip()
    return bool(w) and not _LEADER_RE.match(w)


def pdf_words_and_text(doc):
    """Words in reading order with their page and box, plus the text they spell out.

    The two returned values stay index-aligned: the Nth whitespace-separated word of
    `text` is `words[N]`. The viewer leans on that to know which box to highlight
    while a word is being spoken, so the invariant is worth keeping (test_pdf_text.py).
    """
    paras = []  # each paragraph: list of lines; each line: list of (text, box, page)
    for pno, page in enumerate(doc):
        raw = [w for w in page.get_text("words") if _is_spoken(w[4])]
        raw.sort(key=lambda w: (w[5], w[6], w[7]))  # block, line, word = reading order
        block = line = None
        for x0, y0, x1, y1, wtext, bno, lno, _wno in raw:
            if bno != block:
                paras.append([])
                block, line = bno, None
            if lno != line:
                paras[-1].append([])
                line = lno
            paras[-1][-1].append((wtext, (x0, y0, x1 - x0, y1 - y0), pno))

    words, out = [], []
    for lines in paras:
        flat = []
        for li, line in enumerate(lines):
            nxt = lines[li + 1] if li + 1 < len(lines) else None
            for wi, (wtext, box, pno) in enumerate(line):
                if (wi == len(line) - 1 and nxt and len(wtext) > 1
                        and wtext.endswith("-") and nxt[0][0][:1].islower()):
                    # A word split across a line break, or it gets spoken as two non-words.
                    # ponytail: the highlight lands on the first half; a two-box highlight
                    # would be exact but this reads fine at a glance.
                    wtext = wtext[:-1] + nxt.pop(0)[0]
                flat.append((wtext, box, pno))
        if flat:
            out.append(flat)
            words.extend({"p": pno, "b": [round(v, 1) for v in box]} for _t, box, pno in flat)
    text = "\n\n".join(" ".join(w[0] for w in flat) for flat in out)
    return text, words


MAX_PDFS = 5  # kept for the viewer; anything a queue item still needs is kept regardless


def ingest_pdf(f):
    """Parse an upload into the payload the viewer needs, and keep the file for rendering.

    Raises ValueError carrying a message that is fit to show in the status bar.
    """
    if not f or not f.filename:
        raise ValueError("No file uploaded.")
    try:
        import pymupdf  # PyMuPDF — only the PDF paths need it, so the import stays local
    except ImportError:
        raise ValueError("PDF support needs PyMuPDF:  uv pip install pymupdf")
    blob = f.read()
    try:
        with pymupdf.open(stream=blob, filetype="pdf") as doc:
            if doc.needs_pass:
                raise ValueError("That PDF is password-protected.")
            text, words = pdf_words_and_text(doc)
            pages = [{"w": round(pg.rect.width, 1), "h": round(pg.rect.height, 1)} for pg in doc]
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Could not read that PDF: {exc}")
    if not text:
        raise ValueError("No text in that PDF — it is probably scanned images.")

    # Pages are rendered from this file on demand rather than up front, so a 300-page
    # book opens as fast as a 3-page one.
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:12]
    (PDF_DIR / f"{token}.pdf").write_bytes(blob)
    return {"token": token, "text": text, "words": words,
            "pages": pages, "chars": len(text)}


def prune_pdfs(keep=()):
    """Drop stored PDFs that nothing needs: not queued, not one of the recent few."""
    wanted = {it["token"] for it in queue_items.values() if it.get("token")} | set(keep)
    files = sorted(PDF_DIR.glob("*.pdf"), key=lambda f: f.stat().st_mtime, reverse=True)
    for i, f in enumerate(files):
        if f.stem not in wanted and i >= MAX_PDFS:
            f.unlink(missing_ok=True)


@app.route("/api/extract_pdf", methods=["POST"])
def api_extract_pdf():
    try:
        doc = ingest_pdf(request.files.get("file"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    prune_pdfs({doc["token"]})
    return jsonify(doc)


PAGE_ZOOM = 2  # render at 2x, display at 1x, so pages stay sharp on a HiDPI screen


@app.route("/api/pdf_page/<token>/<int:n>.png")
def api_pdf_page(token, n):
    if not re.fullmatch(r"[0-9a-f]{12}", token):  # it is about to become a file path
        return jsonify({"error": "Bad token."}), 400
    src = PDF_DIR / f"{token}.pdf"
    if not src.exists():
        return jsonify({"error": "That PDF is no longer loaded — open it again."}), 404
    import pymupdf
    with pymupdf.open(src) as doc:
        if not 0 <= n < len(doc):
            return jsonify({"error": "No such page."}), 404
        png = doc[n].get_pixmap(matrix=pymupdf.Matrix(PAGE_ZOOM, PAGE_ZOOM)).tobytes("png")
    return Response(png, mimetype="image/png",
                    headers={"Cache-Control": "public, max-age=3600"})


MAX_QUEUE = 20  # finished items kept before the oldest are cleared out


def queue_view(item):
    """The shape the panel on the right renders. Percent is by characters spoken, which
    tracks steadily even though chunks vary wildly in length."""
    job = jobs.get(item["job"]) if item["job"] else None
    if item["status"] == "done":
        percent = 100
    elif job and job.get("chars_total"):
        percent = min(99, int(job["chars_done"] / job["chars_total"] * 100))
    else:
        percent = 0
    return {"id": item["id"], "name": item["name"], "status": item["status"],
            "pages": len(item["pages"]), "chars": item["chars"], "percent": percent,
            "file": item["file"], "total": item["total"], "error": item["error"],
            "voice": item["voice"]}


def drop_queue_item(qid):
    item = queue_items.pop(qid, None)
    if not item:
        return
    if qid in queue_order:
        queue_order.remove(qid)
    if item["job"]:
        job = jobs.get(item["job"])
        if job and job["status"] == "running":
            job["cancel"] = True      # run_job checks this between chunks
        else:
            for f in CHUNK_DIR.glob(f"{item['job']}_*.wav"):
                f.unlink(missing_ok=True)
            jobs.pop(item["job"], None)
    (PDF_DIR / f"{item['token']}.pdf").unlink(missing_ok=True)


def prune_queue():
    finished = [q for q in queue_order
                if queue_items[q]["status"] in ("done", "error", "cancelled")]
    while len(queue_order) > MAX_QUEUE and finished:
        drop_queue_item(finished.pop(0))


def queue_worker():
    """One reader, one document at a time. Kokoro holds a global lock during generation
    anyway, so running these in parallel would buy nothing."""
    while True:
        qid = work_queue.get()
        item = queue_items.get(qid)
        if not item or item["status"] != "waiting":
            continue
        item["status"] = "running"
        job_id = item["job"] = new_job()
        run_job(job_id, item["text"], item["voice"], item["voice2"],
                item["balance"], item["speed"])
        job = jobs[job_id]
        if qid not in queue_items:       # removed while it was being read
            continue
        item["status"] = job["status"] if job["status"] != "running" else "error"
        item["file"] = job.get("file")
        item["total"] = job.get("total")
        item["error"] = job.get("error")


def start():
    """Clear last session's scratch files and start the queue reader. Called by
    whoever serves the app, never on import: the tests and bootstrap's warm step
    import this module, and must not wipe a running session's chunks and PDFs."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(CHUNK_DIR, ignore_errors=True)
    CHUNK_DIR.mkdir()
    shutil.rmtree(PDF_DIR, ignore_errors=True)
    PDF_DIR.mkdir()
    threading.Thread(target=queue_worker, daemon=True).start()


@app.route("/api/queue", methods=["GET", "POST"])
def api_queue():
    if request.method == "GET":
        return jsonify({"items": [queue_view(queue_items[q]) for q in queue_order]})

    files = [f for f in request.files.getlist("file") if f and f.filename]
    if not files:
        return jsonify({"error": "No file uploaded."}), 400
    voice = request.form.get("voice") or "af_heart"
    voice2 = request.form.get("voice2") or None
    balance = float(request.form.get("balance") or 0.5)
    speed = float(request.form.get("speed") or 1.0)

    added, errors = 0, []
    for f in files:
        try:
            doc = ingest_pdf(f)
        except ValueError as exc:
            errors.append(f"{f.filename or 'file'}: {exc}")
            continue
        qid = uuid.uuid4().hex[:8]
        queue_items[qid] = {"id": qid, "name": f.filename, "status": "waiting",
                            "created": time.time(), "job": None, "file": None,
                            "total": None, "error": None, "voice": voice,
                            "voice2": voice2, "balance": balance, "speed": speed,
                            **doc}
        queue_order.append(qid)
        work_queue.put(qid)
        added += 1

    prune_queue()
    prune_pdfs()
    return jsonify({"added": added, "errors": errors,
                    "items": [queue_view(queue_items[q]) for q in queue_order]})


@app.route("/api/queue/<qid>", methods=["DELETE"])
def api_queue_remove(qid):
    if qid not in queue_items:
        return jsonify({"error": "No such item."}), 404
    drop_queue_item(qid)
    return jsonify({"items": [queue_view(queue_items[q]) for q in queue_order]})


@app.route("/api/queue/<qid>/doc")
def api_queue_doc(qid):
    """Everything needed to reopen a finished document: its pages, its word boxes and
    the timed chunks, so it lands in the viewer already synced."""
    item = queue_items.get(qid)
    if not item:
        return jsonify({"error": "No such item."}), 404
    if item["status"] != "done":
        return jsonify({"error": "That one is not finished yet."}), 409
    job = jobs.get(item["job"])
    if not job or not job["chunks"]:
        return jsonify({"error": "Its audio was cleared — generate it again."}), 410
    return jsonify({"name": item["name"], "file": item["file"], "total": item["total"],
                    "chunks": job["chunks"],
                    "doc": {"token": item["token"], "text": item["text"],
                            "words": item["words"], "pages": item["pages"],
                            "chars": item["chars"]}})


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
  #pdfBtn { margin-left: auto; }
  #editBtn { display: none; }
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

  #pdfview { flex: 1; display: none; overflow-y: auto; padding: 20px 40px 40px; }
  .pdfpage {
    position: relative; margin: 0 auto 18px; max-width: 100%;
    border: 1px solid var(--border); border-radius: 2px; background: #ffffff;
    box-shadow: 0 2px 10px rgba(17, 24, 39, .07);
  }
  .pdfpage img { display: block; width: 100%; height: auto; border-radius: 2px; }
  /* A highlighter pen: multiply keeps the page's own text readable through it. */
  #pdfHL {
    position: absolute; display: none; pointer-events: none; border-radius: 2px;
    background: #ffd54a; mix-blend-mode: multiply;
    transition: left .1s linear, top .1s linear, width .1s linear, height .1s linear;
  }
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
  #qAdd { width: 100%; margin-bottom: 4px; }
  .q-item { padding: 10px 0; border-bottom: 1px solid var(--border); font-size: 13px; }
  .q-item .name { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .q-item .meta { display: flex; gap: 10px; align-items: center; color: var(--faint);
    font-size: 12px; margin-top: 4px; }
  .q-item .act { background: none; border: 0; padding: 0; font: inherit; font-size: 12px;
    color: var(--text); text-decoration: underline; cursor: pointer; }
  .q-item a.act { text-decoration: underline; }
  .q-item .first { margin-left: auto; }
  .q-bar { height: 4px; background: var(--border); border-radius: 2px; margin-top: 7px;
    overflow: hidden; }
  .q-bar i { display: block; height: 100%; width: 0; background: var(--black);
    transition: width .4s ease; }
  .q-state { font-size: 10px; font-weight: 600; letter-spacing: .7px; text-transform: uppercase; }
  .q-state.done { color: #15803d; }
  .q-state.error, .q-state.cancelled { color: var(--danger); }
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
      <input type="file" id="pdfFile" accept="application/pdf,.pdf" hidden>
      <button class="small" id="pdfBtn">Open PDF</button>
      <button class="small" id="editBtn">Edit text</button>
    </div>
    <textarea id="text" placeholder="Start typing or paste anything here — a sentence or a whole chapter. Long texts stream: playback begins while the rest is still generating, and each word lights up as it's spoken."></textarea>
    <div id="reader"></div>
    <div id="pdfview"><div id="pdfHL"></div></div>
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
      <h2>Reading queue</h2>
      <input type="file" id="qFiles" accept="application/pdf,.pdf" multiple hidden>
      <button class="small" id="qAdd">Add PDFs…</button>
      <div id="queue"></div>
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
let wordEls = [], lastNow = -1, generating = false;
let pdfDoc = null, pdfNorm = [], pdfCursor = 0, pdfPages = [], lastPw = -1;
let viewMode = "edit";   // edit | reader | pdf
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

function setView(m) {
  viewMode = m;
  $("text").style.display    = m === "edit"   ? "block" : "none";
  $("reader").style.display  = m === "reader" ? "block" : "none";
  $("pdfview").style.display = m === "pdf"    ? "block" : "none";
  $("editBtn").style.display = m === "edit"   ? "none"  : "inline-block";
}
// A loaded PDF is its own reading view; without one we fall back to the text reader.
function showReader(on) { setView(on ? (pdfDoc ? "pdf" : "reader") : "edit"); }

function normWord(s) { return (s || "").toLowerCase().replace(/[^\p{L}\p{N}]/gu, ""); }

const PDF_LOOKAHEAD = 8;
function alignWord(txt) {
  // Point a spoken token at a word box in the PDF. Exact match with a short lookahead;
  // when nothing matches we hold position, so a token with no word on the page (a spelled
  // out number, say) leaves the highlight where it is and the next real word re-syncs it.
  if (!pdfDoc) return -1;
  const key = normWord(txt);
  if (key) {
    for (let k = 0; k < PDF_LOOKAHEAD && pdfCursor + k < pdfNorm.length; k++) {
      if (pdfNorm[pdfCursor + k] === key) { pdfCursor += k + 1; return pdfCursor - 1; }
    }
  }
  return pdfCursor - 1;
}

function movePdfHL(i) {
  const w = pdfDoc && pdfDoc.words[i];
  const pageDiv = w && pdfPages[w.p];
  if (!pageDiv || !$("pdfHL")) return;
  const scale = pageDiv.clientWidth / pdfDoc.pages[w.p].w;   // pages shrink on a narrow window
  const hl = $("pdfHL");
  if (hl.parentNode !== pageDiv) pageDiv.appendChild(hl);
  hl.style.left   = (w.b[0] * scale) + "px";
  hl.style.top    = (w.b[1] * scale) + "px";
  hl.style.width  = (w.b[2] * scale) + "px";
  hl.style.height = (w.b[3] * scale) + "px";
  hl.style.display = "block";
  hl.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function buildPdfView(d) {
  const v = $("pdfview");
  v.innerHTML = '<div id="pdfHL"></div>';
  pdfPages = [];
  d.pages.forEach((pg, i) => {
    const div = document.createElement("div");
    div.className = "pdfpage";
    div.dataset.p = i;
    div.style.width = pg.w + "px";
    div.style.aspectRatio = pg.w + " / " + pg.h;  // holds the height before the image lands
    const img = document.createElement("img");
    img.loading = "lazy";                          // pages render server-side as you reach them
    img.src = "/api/pdf_page/" + d.token + "/" + i + ".png";
    img.alt = "Page " + (i + 1);
    div.appendChild(img);
    v.appendChild(div);
    pdfPages.push(div);
  });
}

// Click a word on the page to jump the audio there, same as in the text reader.
$("pdfview").onclick = (e) => {
  const pageDiv = e.target.closest(".pdfpage");
  if (!pageDiv || !pdfDoc || !pdfDoc.words.length || !wordEls.length) return;
  const pno = +pageDiv.dataset.p;
  const r = pageDiv.getBoundingClientRect();
  const scale = pageDiv.clientWidth / pdfDoc.pages[pno].w;
  const x = (e.clientX - r.left) / scale, y = (e.clientY - r.top) / scale;

  // The word under the pointer, else the nearest one on the line that was clicked —
  // landing in the gap between two words should still take you somewhere.
  let hit = -1, best = Infinity;
  for (let i = 0; i < pdfDoc.words.length; i++) {
    const w = pdfDoc.words[i];
    if (w.p !== pno) continue;
    const bx = w.b[0], by = w.b[1], bw = w.b[2], bh = w.b[3];
    const dx = x < bx ? bx - x : (x > bx + bw ? x - (bx + bw) : 0);
    const dy = y < by ? by - y : (y > by + bh ? y - (by + bh) : 0);
    if (dy > bh) continue;                  // a different line entirely
    const d = dx + dy * 4;                  // same line beats a nearer column
    if (d < best) { best = d; hit = i; }
  }
  if (hit < 0 || best > 60) return;

  // A word the voice never spoke as written (a spelled-out number, say) owns no token,
  // so jump to the first token at or after it rather than doing nothing.
  const el = wordEls.find((w) => +w.dataset.pw >= hit) || wordEls[wordEls.length - 1];
  if (el) seekTo(+el.dataset.start + 0.001);
};

$("pdfBtn").onclick = () => $("pdfFile").click();
$("pdfFile").onchange = async (e) => {
  const f = e.target.files[0];
  if (!f) return;
  e.target.value = "";                 // so picking the same file twice still fires
  $("pdfBtn").disabled = true;
  $("status").textContent = "Reading " + f.name + "…";
  try {
    const fd = new FormData(); fd.append("file", f);
    const r = await fetch("/api/extract_pdf", { method: "POST", body: fd });
    const data = await r.json();
    if (data.error) { $("status").innerHTML = '<span class="err">' + data.error + "</span>"; return; }
    audio.pause(); $("play").textContent = "\u25b6";
    pdfDoc = data;
    pdfNorm = data.text.split(/\s+/).map(normWord);   // 1:1 with data.words, by construction
    pdfCursor = 0; lastPw = -1;
    $("text").value = data.text;
    $("text").dispatchEvent(new Event("input"));
    buildPdfView(data);
    setView("pdf");
    $("status").textContent = f.name + " — " + data.pages.length + " page"
      + (data.pages.length === 1 ? "" : "s") + ", " + data.chars.toLocaleString()
      + " characters. Press Generate speech to read it aloud.";
  } catch (err) {
    $("status").innerHTML = '<span class="err">Could not read that PDF: ' + err + "</span>";
  } finally {
    $("pdfBtn").disabled = false;
  }
};

// ---- reading queue --------------------------------------------------------
// PDFs are read one at a time on the server, so you can queue a stack of them and
// come back later. Progress is by characters spoken, not chunks: chunk lengths vary
// hugely (a references section is all tiny ones) and a chunk count crawls unevenly.
let queueTimer = null;

$("qAdd").onclick = () => $("qFiles").click();
$("qFiles").onchange = async (e) => {
  const files = [...e.target.files];
  if (!files.length) return;
  e.target.value = "";
  const fd = new FormData();
  for (const f of files) fd.append("file", f);
  fd.append("voice", $("voice").value);
  if ($("blendOn").checked) fd.append("voice2", $("voice2").value);
  fd.append("balance", +$("balance").value / 100);
  fd.append("speed", +$("speed").value);
  $("qAdd").disabled = true;
  $("status").textContent = "Adding " + files.length + " PDF" + (files.length === 1 ? "" : "s") + "…";
  try {
    const d = await (await fetch("/api/queue", { method: "POST", body: fd })).json();
    if (d.error) { $("status").innerHTML = '<span class="err">' + d.error + "</span>"; return; }
    renderQueue(d.items);
    const msg = d.added + " queued. They are read in the background — you can close this tab's "
      + "document and keep working.";
    $("status").innerHTML = d.errors.length
      ? msg + ' <span class="err">' + d.errors.join("; ") + "</span>" : msg;
  } catch (err) {
    $("status").innerHTML = '<span class="err">Could not add to the queue: ' + err + "</span>";
  } finally {
    $("qAdd").disabled = false;
    pollQueue();
  }
};

function queueAction(label, fn, first) {
  const b = document.createElement("button");
  b.className = "act" + (first ? " first" : "");
  b.textContent = label;
  b.onclick = fn;
  return b;
}

function renderQueue(items) {
  const box = $("queue");
  box.innerHTML = "";
  if (!items.length) {
    box.innerHTML = '<span class="hint">Nothing queued. Add PDFs and they are read one '
      + 'after another while you get on with something else.</span>';
    return;
  }
  for (const it of items) {
    const div = document.createElement("div");
    div.className = "q-item";

    const name = document.createElement("div");
    name.className = "name";
    name.textContent = it.name;            // a filename is not markup
    name.title = it.name;
    div.appendChild(name);

    if (it.status === "waiting" || it.status === "running") {
      const bar = document.createElement("div");
      bar.className = "q-bar";
      const fill = document.createElement("i");
      fill.style.width = it.percent + "%";
      bar.appendChild(fill);
      div.appendChild(bar);
    }

    const meta = document.createElement("div");
    meta.className = "meta";
    const state = document.createElement("span");
    state.className = "q-state " + it.status;
    state.textContent = it.status === "running" ? "reading " + it.percent + "%" : it.status;
    meta.appendChild(state);

    const size = document.createElement("span");
    size.textContent = it.pages + (it.pages === 1 ? " page" : " pages")
      + (it.total ? " · " + fmt(it.total) : "");
    meta.appendChild(size);

    if (it.status === "done") {
      meta.appendChild(queueAction("open", () => openQueueItem(it.id), true));
      const a = document.createElement("a");
      a.className = "act";
      a.href = "/outputs/" + it.file;
      a.textContent = "download";
      meta.appendChild(a);
      meta.appendChild(queueAction("remove", () => removeQueueItem(it.id)));
    } else {
      meta.appendChild(queueAction(it.status === "running" ? "stop" : "remove",
                                   () => removeQueueItem(it.id), true));
    }
    div.appendChild(meta);

    if (it.error) {
      const err = document.createElement("div");
      err.className = "hint";
      err.textContent = it.error;
      div.appendChild(err);
    }
    box.appendChild(div);
  }
}

async function pollQueue() {
  clearTimeout(queueTimer);
  try {
    const d = await (await fetch("/api/queue")).json();
    renderQueue(d.items);
    if (d.items.some((i) => i.status === "waiting" || i.status === "running")) {
      queueTimer = setTimeout(pollQueue, 1500);
    }
  } catch (err) {
    queueTimer = setTimeout(pollQueue, 4000);   // server restarting, most likely
  }
}

async function removeQueueItem(id) {
  const d = await (await fetch("/api/queue/" + id, { method: "DELETE" })).json();
  if (d.error) { $("status").innerHTML = '<span class="err">' + d.error + "</span>"; return; }
  renderQueue(d.items);
  pollQueue();
}

// Reopen a finished document straight into the synced page view.
async function openQueueItem(id) {
  const d = await (await fetch("/api/queue/" + id + "/doc")).json();
  if (d.error) { $("status").innerHTML = '<span class="err">' + d.error + "</span>"; return; }
  audio.pause(); $("play").textContent = "\u25b6";
  chunks = []; curIdx = -1; totalDur = d.total; jobDone = true; wordEls = []; lastNow = -1;
  curPara = null; firstInPara = true;
  $("reader").innerHTML = "";
  pdfDoc = d.doc;
  pdfNorm = d.doc.text.split(/\s+/).map(normWord);
  pdfCursor = 0; lastPw = -1;
  $("text").value = d.doc.text;
  $("text").dispatchEvent(new Event("input"));
  buildPdfView(d.doc);
  for (const c of d.chunks) { chunks.push(c); renderChunk(c); }
  $("play").disabled = false;
  $("go").disabled = false;
  $("fileActions").style.display = "flex";
  $("dlWav").href = "/outputs/" + d.file;
  $("mp3Btn").dataset.file = d.file;
  setView("pdf");
  $("status").textContent = d.name + " — ready, " + fmt(d.total) + ". Press play, or click any word.";
}

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
    span.dataset.pw = alignWord(txt);
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
    if (viewMode === "pdf") {
      const pw = +nowEl.dataset.pw;
      if (pw >= 0 && pw !== lastPw) { lastPw = pw; movePdfHL(pw); }
    } else {
      nowEl.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
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

function setGenerating(on) {
  generating = on;
  $("go").textContent = on ? "Stop" : "Generate speech";
  $("go").disabled = false;
}

$("go").onclick = async () => {
  if (generating) {
    $("status").textContent = "Stopping…";
    try { await fetch("/api/job/" + jobId + "/stop", { method: "POST" }); } catch (e) {}
    return;
  }
  const text = $("text").value.trim();
  if (!text) { $("status").innerHTML = '<span class="err">Enter some text first.</span>'; return; }
  // Edited text no longer lines up with the page boxes, so drop back to the text reader.
  if (pdfDoc && $("text").value !== pdfDoc.text) pdfDoc = null;
  audio.pause();
  chunks = []; curIdx = -1; totalDur = 0; jobDone = false; wordEls = []; lastNow = -1;
  curPara = null; firstInPara = true;
  pdfCursor = 0; lastPw = -1;
  if ($("pdfHL")) $("pdfHL").style.display = "none";   // absent until a PDF is opened
  $("reader").innerHTML = ""; $("fileActions").style.display = "none";
  $("dlMp3").style.display = "none"; $("play").disabled = true;
  setGenerating(true);
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
  if (data.error) { $("status").innerHTML = '<span class="err">' + data.error + "</span>"; setGenerating(false); showReader(false); return; }
  jobId = data.job;
  if (data.behind) {
    $("status").textContent = "Waiting for the queue to finish " + data.behind
      + " — this starts straight after.";
  }
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
    } else if (data.status === "cancelled") {
      clearInterval(poller);
      jobDone = true;
      setGenerating(false);
      $("status").textContent = "Stopped after " + chunks.length + " chunk"
        + (chunks.length === 1 ? "" : "s") + ". What was generated is still playable.";
    } else if (data.status === "done") {
      clearInterval(poller);
      jobDone = true; totalDur = data.total;
      $("status").textContent = "Done — saved as outputs/" + data.file + " (" + fmt(data.total) + ")";
      setGenerating(false);
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
  setGenerating(false);
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
pollQueue();
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
    start()
    url = "http://127.0.0.1:7500"
    print(f"\n  Kokoro TTS Studio → {url}\n  Files are saved to: {OUTPUT_DIR}\n")
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=7500, debug=False)