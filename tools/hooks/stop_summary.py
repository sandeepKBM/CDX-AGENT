#!/usr/bin/env python3
"""Stop hook: emit a working-tree summary at most once per actual change.

Claude Code treats any Stop-hook context emission as "block the stop so the
model can respond to it". The original version of this script emitted on
every Stop event unconditionally, which force-continued every turn and
re-fired on the next stop -- an infinite loop capped only by the harness's
consecutive-block limit. Three rules prevent that now:

1. `stop_hook_active` in the payload means this stop is already a
   continuation caused by a Stop hook -- always allow it through silently.
2. Nothing to say (clean tree, non-git cwd, git timeout) -> no output.
3. A per-session digest of (status, diff-stat) is kept in a state file next
   to the invoked script (the per-runtime hooks/ dir, NOT resolved through
   the symlink, so state stays per-runtime); an unchanged tree never gets
   re-summarized. State I/O failures fall back to emitting -- rule 1 still
   bounds the worst case at one continuation.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

GIT_TIMEOUT_SECONDS = 10
STATE_FILENAME = ".stop_summary_state.json"
MAX_TRACKED_SESSIONS = 50


def _load_payload() -> dict[str, Any]:
    raw = sys.stdin.read()
    return json.loads(raw) if raw.strip() else {}


def _git(cwd: Path | str, *args: str) -> str | None:
    """Run git with a hard timeout; None on any failure (a stuck git, e.g. a
    held index.lock on a huge repo, must never stall the agent's stop path)."""
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _state_path() -> Path:
    # .absolute(), not .resolve(): the invoked path is a per-runtime symlink
    # into codex_tools/hooks, and resolving it would share one state file
    # across every runtime on the box.
    return Path(sys.argv[0]).absolute().parent / STATE_FILENAME


def _read_state() -> dict[str, str]:
    try:
        data = json.loads(_state_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(state: dict[str, str]) -> None:
    if len(state) > MAX_TRACKED_SESSIONS:
        state = dict(list(state.items())[-MAX_TRACKED_SESSIONS:])
    try:
        _state_path().write_text(json.dumps(state))
    except OSError:
        pass


def main() -> int:
    # Disabled at the user's explicit request (2026-08-13): the per-turn
    # "Session stop summary" / diff-stat injection was reported as noisy and
    # a waste of tokens. Left as a no-op (rather than deleting the file or
    # the Stop hook wiring) so this can be re-enabled by removing this early
    # return if ever wanted again.
    return 0

    payload = _load_payload()  # noqa: unreachable while disabled above
    if payload.get("stop_hook_active"):
        return 0

    cwd = Path(str(payload.get("cwd") or ".")).resolve()
    repo = _git(cwd, "rev-parse", "--show-toplevel")
    if not repo:
        return 0

    status = _git(repo, "status", "--short")
    if not status:
        return 0
    diff_stat = _git(repo, "diff", "--stat") or ""

    session_id = str(payload.get("session_id") or "unknown-session")
    digest = hashlib.sha256(f"{status}\n{diff_stat}".encode()).hexdigest()
    state = _read_state()
    if state.get(session_id) == digest:
        return 0
    state[session_id] = digest
    _write_state(state)

    message_parts = ["Session stop summary:", "Changed files:", status]
    if diff_stat:
        message_parts.extend(["", "Diff stat:", diff_stat])
    message_parts.extend(["", "Reminder: include rollback instructions and list tests run/not run."])
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "Stop",
                    "additionalContext": "\n".join(message_parts),
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        # A crashing Stop hook blocks the agent from ending its turn; never crash.
        raise SystemExit(0)
