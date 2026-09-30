"""Self-check for reading a web page aloud.  Run:  env\\Scripts\\python.exe test_url_text.py

No network: extraction runs on a saved page, and the fetch guards are checked against a
throwaway server on 127.0.0.1.
"""

import http.server
import os
import sys
import tempfile
import threading

os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="flowstudio_selftest_")

import app

assert "selftest" in str(app.DATA_DIR), f"test would clobber live data at {app.DATA_DIR}"

# A news page as it arrives: cookie banner, nav, an ad, the article, comments, footer.
PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Harbour seals return to the Thames | Daily Example</title>
<meta property="og:title" content="Harbour seals return to the Thames">
</head><body>
<div id="cookie-banner" class="cookie-consent">We use cookies to improve your experience.
  <button>Accept all cookies</button></div>
<header><nav><ul><li><a href="/">Home</a></li><li><a href="/world">World</a></li>
  <li><a href="/sport">Sport</a></li><li><a href="/subscribe">Subscribe now</a></li></ul></nav></header>
<div class="ad advert">Buy one mattress, get the second one free today only.</div>
<main><article>
  <h1>Harbour seals return to the Thames</h1>
  <p class="byline">By Jane Doe, 3 March 2026</p>
  <p>Harbour seals have been counted in record numbers along the Thames estuary this winter,
  according to a survey published by conservation researchers on Tuesday morning.</p>
  <p>The count found more than nine hundred animals resting on sandbanks between the
  estuary mouth and the city, a rise of almost a third on the figure from five years ago.</p>
  <p>Researchers credit cleaner water and a recovering fish population for the change, and
  say the river now supports one of the largest colonies anywhere in the south of England.</p>
</article></main>
<section id="comments" class="comments"><h2>Comments</h2>
  <div class="comment">First! Great article, loved the seals.</div></section>
<footer><p>Copyright 2026 Daily Example. All rights reserved.</p>
  <a href="/privacy">Privacy policy</a></footer>
</body></html>"""


def test_article_extracted_boilerplate_gone():
    title, text = app.article_from_html(PAGE, "https://news.example/seals")

    assert title == "Harbour seals return to the Thames", title
    assert "nine hundred animals" in text and "largest colonies" in text, text
    for junk in ("cookies", "Subscribe now", "mattress", "First! Great article",
                 "All rights reserved", "Privacy policy"):
        assert junk not in text, (junk, text)
    # one paragraph per blank line, which is how the reader splits paragraphs
    assert text.count("\n\n") >= 2, repr(text)


def test_empty_page():
    empty = "<html><body><nav><a href='/'>Home</a></nav><script>app()</script></body></html>"
    try:
        app.article_from_html(empty)
    except ValueError as exc:
        assert "No article text" in str(exc), exc
    else:
        raise AssertionError("an empty page should say so")


def test_missing_trafilatura_gives_install_hint():
    saved = sys.modules.get("trafilatura")
    sys.modules["trafilatura"] = None          # makes `import trafilatura` fail
    try:
        app.article_from_html(PAGE)
    except ValueError as exc:
        assert "uv pip install trafilatura" in str(exc), exc
    else:
        raise AssertionError("missing trafilatura should give an install hint")
    try:
        expect_error("https://news.example/seals", "uv pip install trafilatura")  # before any fetch
    finally:
        if saved is None:
            sys.modules.pop("trafilatura", None)
        else:
            sys.modules["trafilatura"] = saved


def expect_error(url, fragment):
    try:
        app.ingest_url(url)
    except ValueError as exc:
        assert fragment in str(exc), (url, str(exc))
    else:
        raise AssertionError(f"{url} should have failed")


def test_bad_urls():
    expect_error("", "not a web address")
    expect_error("file:///etc/passwd", "not a web address")
    expect_error("news.example/seals", "not a web address")
    expect_error("http://", "not a web address")


class Handler(http.server.BaseHTTPRequestHandler):
    routes = {
        "/article": ("text/html; charset=utf-8", PAGE.encode()),
        "/pdf": ("application/pdf", b"%PDF-1.4"),
        "/huge": ("text/html", b"<p>" + b"x" * (app.URL_MAX_BYTES + 10)),
    }

    def do_GET(self):
        if self.path not in self.routes:
            self.send_error(404, "Not Found")
            return
        ctype, body = self.routes[self.path]
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_fetch_over_loopback():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        doc = app.ingest_url(base + "/article")
        assert doc["title"] == "Harbour seals return to the Thames", doc
        assert doc["text"].startswith(doc["title"] + "\n\n"), doc["text"][:80]
        assert doc["chars"] == len(doc["text"])
        assert "cookies" not in doc["text"]

        expect_error(base + "/pdf", "not a web page (application/pdf)")
        expect_error(base + "/huge", "too big")
        expect_error(base + "/missing", "404")

        # queued next to PDFs; a web item has no pages and no file to clean up
        client = app.app.test_client()
        d = client.post("/api/queue", data={"url": [base + "/article", base + "/pdf"]}).get_json()
        assert d["added"] == 1 and len(d["errors"]) == 1, d
        item = d["items"][-1]
        assert item["name"] == "Harbour seals return to the Thames" and item["pages"] == 0, item
        assert client.delete("/api/queue/" + item["id"]).status_code == 200
    finally:
        srv.shutdown()
        srv.server_close()
    expect_error(base + "/article", "Could not reach")   # server gone: what offline looks like


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok  ", name)
    print("URL self-check passed")
