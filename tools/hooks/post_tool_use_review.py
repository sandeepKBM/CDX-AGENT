#!/usr/bin/env python3
"""PostToolUse hook: report git working-tree state only when it changes.

The original version emitted "Git working tree changed after shell command."
plus a full `git status --short` after every non-read-only Bash call, even
when nothing had changed -- pure repeated noise in long sessions. Now a
per-session digest of (status, diff-stat) is kept in a state file next to the
invoked script (the per-runtime hooks/ dir, NOT resolved through the symlink,
so state stays per-runtime) and the git block is emitted only on an actual
transition. Token-saving hints for noisy commands are independent of git
state and unchanged. All git calls have a hard timeout so a stuck git (held
index.lock, huge repo) never stalls the agent's tool loop.

2026-10-16: the transition block is now ONE summary line (counts per status
class + top-level dirs + diff line totals) instead of the full
`git status --short` + `git diff --stat` (20-35 KB per call in big repos),
and the token-saving tips are shown at most once per session (tips state
file next to the state file, falling back to $TMPDIR if unwritable).
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from collections import Counter
from pathlib import Path
from typing import Any

GIT_TIMEOUT_SECONDS = 10
STATE_FILENAME = ".post_review_state.json"
TIPS_STATE_FILENAME = ".post_review_tips_state.json"
MAX_TRACKED_SESSIONS = 50
LARGE_OUTPUT_THRESHOLD = 8000
LARGE_OUTPUT_STATE_FILENAME = ".post_review_large_output_state.json"
RESTART_NUDGE_STATE_FILENAME = ".post_review_restart_nudge_state.json"
OUTPUT_STATS_FILENAME = ".tool_output_stats.jsonl"
OUTPUT_STATS_MAX_BYTES = 5 * 1024 * 1024
TOP_DIRS = 3

READ_ONLY_PREFIXES = (
    "ls",
    "pwd",
    "cat",
    "grep",
    "rg",
    "find",
    "head",
    "tail",
    "sed",
    "which",
    "type",
    "alias",
    "echo",
    "git status",
    "git diff",
)

NOISY_COMMAND_HINTS = (
    "ls -R",
    "find ",
    "grep -R",
    "git diff",
    "pytest",
    "train",
    "eval",
)


def _load_payload() -> dict[str, Any]:
    raw = sys.stdin.read()
    return json.loads(raw) if raw.strip() else {}


def _tool_command(payload: dict[str, Any]) -> str:
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        for key in ("command", "cmd", "patch", "text"):
            value = tool_input.get(key)
            if isinstance(value, str):
                return value.strip()
    if isinstance(tool_input, str):
        return tool_input.strip()
    return ""


def _git(cwd: Path | str, *args: str) -> str | None:
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


def _tool_output_text(payload: dict[str, Any]) -> str:
    def collect(value: Any) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            parts: list[str] = []
            for item in value:
                parts.extend(collect(item))
            return parts
        if isinstance(value, dict):
            parts = []
            for key in ("stdout", "stderr", "output", "text", "content", "result", "data"):
                if key in value:
                    parts.extend(collect(value[key]))
            return parts
        return []

    parts: list[str] = []
    for key in ("tool_output", "tool_response", "output", "result", "stdout", "stderr"):
        if key in payload:
            parts.extend(collect(payload[key]))
    return "".join(parts)


def _state_path() -> Path:
    # .absolute(), not .resolve(): the invoked path is a per-runtime symlink
    # into codex_tools/hooks; resolving would share state across all runtimes.
    return Path(sys.argv[0]).absolute().parent / STATE_FILENAME


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, state: dict[str, Any]) -> bool:
    """Atomic write (tmp + rename) so parallel hook runs never leave torn JSON."""
    if len(state) > MAX_TRACKED_SESSIONS:
        state = dict(list(state.items())[-MAX_TRACKED_SESSIONS:])
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(state))
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def _read_state() -> dict[str, Any]:
    return _read_json(_state_path())


def _write_state(state: dict[str, Any]) -> None:
    _write_json(_state_path(), state)


def _tips_paths() -> list[Path]:
    primary = _state_path().with_name(TIPS_STATE_FILENAME)
    runtime_tag = hashlib.sha256(str(primary.parent).encode()).hexdigest()[:12]
    fallback = Path(tempfile.gettempdir()) / f"post_review_tips_{os.getuid()}_{runtime_tag}.json"
    return [primary, fallback]


def _tips_first_time(session_id: str) -> bool:
    """True exactly once per session (best effort: if no state file is
    writable anywhere, tips keep showing, i.e. the old behavior)."""
    paths = _tips_paths()
    if any(session_id in _read_json(path) for path in paths):
        return False
    for path in paths:
        state = _read_json(path)
        state[session_id] = 1
        if _write_json(path, state):
            break
    return True


def _large_output_state_path() -> Path:
    return _state_path().with_name(LARGE_OUTPUT_STATE_FILENAME)


def _large_output_first_time(session_id: str) -> bool:
    path = _large_output_state_path()
    state = _read_json(path)
    if session_id in state:
        return False
    state.pop(session_id, None)
    state[session_id] = 1
    if len(state) > 200:
        state = dict(list(state.items())[-200:])
    _write_json(path, state)
    return True


def _restart_nudge(payload: dict[str, Any], session_id: str) -> str | None:
    transcript_value = payload.get("transcript_path")
    if not isinstance(transcript_value, str) or not transcript_value:
        return None
    transcript = Path(transcript_value)
    if not transcript.is_file():
        return None
    threshold_mb = float(os.environ.get("CDX_RESTART_NUDGE_MB", "1.5"))
    if threshold_mb <= 0:
        return None
    size = transcript.stat().st_size
    threshold_bytes = threshold_mb * 1024 * 1024
    level = min(3, int(size // threshold_bytes))
    if level < 1:
        return None

    path = _state_path().with_name(RESTART_NUDGE_STATE_FILENAME)
    state = _read_json(path)
    previous = state.get(session_id, 0)
    try:
        previous_level = int(previous)
    except (TypeError, ValueError):
        previous_level = 0
    if level <= previous_level:
        return None
    state[session_id] = level
    _write_json(path, state)
    size_mb = size / (1024 * 1024)
    tokens = max(1, size // 4)
    return (
        f"This session's transcript is ~{size_mb:.1f} MB (~{tokens} tokens): at the next natural breakpoint "
        "write `cdx-agent --handoff --notes NOTES.md` and continue in a fresh session "
        "(every turn re-sends the whole context)."
    )


def _append_output_stats(session_id: str, tool_name: str, chars: int) -> None:
    path = _state_path().with_name(OUTPUT_STATS_FILENAME)
    record = {
        "t": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "session": session_id,
        "tool": tool_name,
        "chars": chars,
    }
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        if path.stat().st_size > OUTPUT_STATS_MAX_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))
            path.touch()
    except OSError:
        return


def _status_path(line: str) -> str:
    path = line[3:]
    if " -> " in path:
        path = path.split(" -> ", 1)[1]
    return path.strip().strip('"')


def _status_class(xy: str) -> str:
    if xy == "??":
        return "untracked"
    if "U" in xy or xy in ("AA", "DD"):
        return "conflicted"
    if xy[0] == "A":
        return "added"
    if "D" in xy:
        return "deleted"
    if xy[0] in "RC":
        return "renamed"
    return "modified"


CLASS_ORDER = ("modified", "added", "deleted", "renamed", "conflicted", "untracked")


def _summary_line(status: str, diff_stat: str, header: str) -> str:
    classes: Counter[str] = Counter()
    dirs: Counter[str] = Counter()
    for line in status.splitlines():
        if len(line) < 4:
            continue
        classes[_status_class(line[:2])] += 1
        path = _status_path(line)
        dirs[path.split("/", 1)[0] + "/" if "/" in path else "(root)"] += 1
    parts = [f"{classes[name]} {name}" for name in CLASS_ORDER if classes[name]]
    text = f"{header}: " + ", ".join(parts)
    if dirs:
        ranked = sorted(dirs.items(), key=lambda item: (-item[1], item[0]))[:TOP_DIRS]
        top = ", ".join(f"{name} {count}" for name, count in ranked)
        text += f" (top: {top})"
    totals = diff_stat.splitlines()[-1].strip() if diff_stat else ""
    ins = next((w for w in totals.split(", ") if "insertion" in w), "")
    dels = next((w for w in totals.split(", ") if "deletion" in w), "")
    if ins or dels:
        text += f"; unstaged +{ins.split(' ')[0] if ins else 0}/-{dels.split(' ')[0] if dels else 0} lines"
    return text


def _git_state_lines(payload: dict[str, Any], cwd: Path) -> list[str]:
    # Routine "worktree baseline/changed" notes are off by default (noise after
    # nearly every Bash call); set CDX_WORKTREE_NOTES=1 to restore them.
    if os.environ.get("CDX_WORKTREE_NOTES", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return []
    repo = _git(cwd, "rev-parse", "--show-toplevel")
    if not repo:
        return []
    status = _git(repo, "status", "--porcelain")
    diff_stat = _git(repo, "diff", "--stat")
    if status is None or diff_stat is None:
        return []

    session_id = str(payload.get("session_id") or "unknown-session")
    digest = hashlib.sha256(f"{status}\n{diff_stat}".encode()).hexdigest()
    state = _read_state()
    previous = state.get(session_id)
    if digest == previous:
        return []
    state[session_id] = digest
    _write_state(state)

    if not status:
        # Transition back to a clean tree: worth updating state, not narrating.
        return []
    header = "worktree changed" if previous is not None else "worktree baseline"
    return [_summary_line(status, diff_stat, header)]


def main() -> int:
    payload = _load_payload()
    tool_name = str(payload.get("tool_name") or "")
    session_id = str(payload.get("session_id") or "unknown-session")
    lines = []
    if tool_name == "Bash":
        output_text = _tool_output_text(payload)
        _append_output_stats(session_id, tool_name, len(output_text))
        command = _tool_command(payload)
        cwd = Path(str(payload.get("cwd") or ".")).resolve()
        if not any(command.startswith(prefix) for prefix in READ_ONLY_PREFIXES):
            lines = _git_state_lines(payload, cwd)

        output_line_count = len(output_text.splitlines()) if output_text else 0
        noisy = output_line_count > 400 or any(hint in command for hint in NOISY_COMMAND_HINTS)
        if noisy and _tips_first_time(session_id):
            lines.append(
                "Token tips (shown once per session): cdx-agent --compress-output <cmd> for noisy "
                "output; --summarize-log <file> for big logs; --context-budget when exploration is broad."
            )
        if len(output_text) > LARGE_OUTPUT_THRESHOLD and _large_output_first_time(session_id):
            size_kb = (len(output_text) + 1023) // 1024
            lines.append(
                f"Large output ({size_kb} KB). Mask it next time: cdx-agent --compress-output <cmd>, "
                "or redirect to a file and read head/grep."
            )
    nudge = _restart_nudge(payload, session_id)
    if nudge:
        lines.append(nudge)
    if not lines:
        return 0
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": "\n".join(lines),
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
        # A crashing hook stalls the agent's tool loop; never crash.
        raise SystemExit(0)
