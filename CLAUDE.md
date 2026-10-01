# Flow Studio

Windows desktop app (Python 3.12): TTS (`app.py`), dictation (`flow.py`), shell (`flow_studio.py`), installer bootstrap (`bootstrap.py`).
The full app only runs on Windows.

## Tests

One command: `.agent/test` (stdlib tests plus the URL-reader tests; runs in WSL and in CI on Windows).
`test_pdf_text.py` needs the full Windows requirements, so run it only on Windows.

## Agent skills

### Issue tracker

GitHub issues. See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary (`ready-for-agent` = a worker may take it). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context. See `docs/agents/domain.md`.
