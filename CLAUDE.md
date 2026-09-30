# Flow Studio

Windows desktop app (Python 3.12): TTS (`app.py`), dictation (`flow.py`), shell (`flow_studio.py`), installer bootstrap (`bootstrap.py`).
The full app only runs on Windows. From WSL, run the stdlib-only tests: `uvx pytest -q test_bootstrap.py`.

## Background worker rules

These apply when you run as an agent-view background session working one GitHub issue.

- Work on exactly one issue. Read it first with `gh issue view <n>`. Do not start other issues.
- Stay in your own worktree. Never edit, run commands in, or read other worktrees.
- Never push to `master`. Push only your own branch, then open a **draft** PR whose body says `Closes #<n>`.
- Before opening the PR: rebase on `origin/master`, then run `uvx pytest -q test_bootstrap.py` and make sure it passes.
- Add or update a test when you change logic in `bootstrap.py`.
- If the issue is ambiguous, ask. Do not guess on behaviour the issue does not specify.
- Commit your work before you finish: deleting the session deletes its worktree.
