# Flow Studio

Windows desktop app (Python 3.12): TTS (`app.py`), dictation (`flow.py`), shell (`flow_studio.py`), installer bootstrap (`bootstrap.py`).
The full app only runs on Windows. From WSL, run the stdlib-only tests: `uvx pytest -q test_bootstrap.py`.

## Tests

Run before every PR: `uvx pytest -q test_bootstrap.py` (29+ stdlib-only tests, runs in WSL).
`test_pdf_text.py` needs the full Windows requirements, so run it only on Windows.

Worker sandbox, deny rules and generic worker rules live in `~/projects/herdr-setup` and are passed in by `dispatch`.

## Agent skills

### Issue tracker

GitHub issues. See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary (`ready-for-agent` = a worker may take it). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context. See `docs/agents/domain.md`.
