"""Self-check for the PDF import + page view.  Run:  env\\Scripts\\python.exe test_pdf_text.py

The one invariant worth guarding: the Nth whitespace-separated word of the returned
text is words[N].  The viewer maps a spoken word onto a box with that assumption, so
if it ever breaks the highlight silently drifts onto the wrong word.
"""

import os
import tempfile
import threading

# Importing app touches no files, but point it at a throwaway directory anyway so a
# test that does write can never reach a running session's data.
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="flowstudio_selftest_")

import pymupdf

import app

assert "selftest" in str(app.DATA_DIR), f"test would clobber live data at {app.DATA_DIR}"
assert not app.DATA_DIR.exists(), "importing app must not touch the data dir"
assert not any("queue_worker" in t.name for t in threading.enumerate()), \
    "importing app must not start the queue reader"


def make_pdf(lines_per_page):
    """lines_per_page: [[(x, y, "some words"), ...], ...] — one list per page."""
    doc = pymupdf.open()
    for lines in lines_per_page:
        page = doc.new_page()
        for x, y, txt in lines:
            page.insert_text((x, y), txt, fontsize=11)
    data = doc.tobytes()
    doc.close()
    return pymupdf.open(stream=data, filetype="pdf")


def test_alignment_invariant():
    with make_pdf([
        [(72, 100, "The quick brown fox"), (72, 120, "jumps over the lazy dog")],
        [(72, 100, "Second page here")],
    ]) as doc:
        text, words = app.pdf_words_and_text(doc)

    assert len(text.split()) == len(words), (len(text.split()), len(words))
    assert text.split()[:4] == ["The", "quick", "brown", "fox"], text.split()[:4]
    assert "Second page here" in text
    # page numbers are recorded, and boxes are non-empty
    assert words[0]["p"] == 0 and words[-1]["p"] == 1, (words[0], words[-1])
    assert all(w["b"][2] > 0 and w["b"][3] > 0 for w in words)


def test_reading_order_and_boxes():
    with make_pdf([[(72, 100, "alpha beta"), (72, 130, "gamma")]]) as doc:
        text, words = app.pdf_words_and_text(doc)

    assert text.split() == ["alpha", "beta", "gamma"], text.split()
    # boxes advance left-to-right on the first line, then drop down the page
    assert words[1]["b"][0] > words[0]["b"][0], words
    assert words[2]["b"][1] > words[0]["b"][1], words


def test_hyphen_rejoin():
    """A word broken over a line break is spoken as one word, not two non-words."""
    with make_pdf([[(72, 100, "die Struk-"), (72, 114, "turen sind da")]]) as doc:
        text, words = app.pdf_words_and_text(doc)

    assert "Strukturen" in text, text
    assert "Struk-" not in text, text
    assert len(text.split()) == len(words), (text.split(), len(words))


def test_capitalised_compound_survives():
    """Nord-\\nSee is a real hyphen, not a line break artefact, so it is left alone."""
    with make_pdf([[(72, 100, "die Nord-"), (72, 114, "See ist kalt")]]) as doc:
        text, _ = app.pdf_words_and_text(doc)

    assert "Nord-" in text, text


def test_dot_leaders_dropped():
    """A contents line is spoken as "Results 4", not as a run of dots."""
    with make_pdf([[(72, 100, "Results . . . . . . . 4")]]) as doc:
        text, words = app.pdf_words_and_text(doc)

    assert text.split() == ["Results", "4"], text.split()
    assert len(text.split()) == len(words)


def test_queue_progress():
    """Progress is by characters spoken, and must never read 100% before it is done."""
    app.jobs.clear()
    app.queue_items.clear()
    app.queue_order.clear()
    item = {"id": "x", "name": "a.pdf", "status": "running", "job": "j1", "file": None,
            "total": None, "error": None, "voice": "af_heart", "token": "tok",
            "pages": [{"w": 1, "h": 1}], "chars": 100}
    app.queue_items["x"] = item
    app.jobs["j1"] = {"status": "running", "chunks": [], "created": 0,
                      "chars_total": 100, "chars_done": 50}

    assert app.queue_view(item)["percent"] == 50

    app.jobs["j1"]["chars_done"] = 100          # last chunk in, file not yet written
    assert app.queue_view(item)["percent"] == 99

    item["status"] = "done"
    assert app.queue_view(item)["percent"] == 100

    app.queue_items.clear()


def test_queue_job_survives_eviction():
    """A finished queue item stays playable however many one-off generations follow it."""
    app.jobs.clear()
    app.queue_items.clear()
    app.queue_order.clear()
    app.queue_items["x"] = {"id": "x", "job": "keep", "token": "tok"}
    app.jobs["keep"] = {"status": "done", "chunks": [], "created": 0}
    for n in range(app.MAX_JOBS + 3):
        app.jobs[f"one{n}"] = {"status": "done", "chunks": [], "created": n + 1}

    app._evict_old_jobs()

    assert "keep" in app.jobs, "a queued document's audio was evicted"
    assert len(app.jobs) <= app.MAX_JOBS + 1
    app.queue_items.clear()
    app.jobs.clear()


def test_empty_pdf():
    with make_pdf([[]]) as doc:
        text, words = app.pdf_words_and_text(doc)

    assert text == "" and words == [], (text, words)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok  ", name)
    print("PDF self-check passed")
