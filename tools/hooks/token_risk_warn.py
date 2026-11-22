#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


ADVISORY_STATE_FILENAME = ".risk_advisory_state.json"
MAX_ADVISORY_SESSIONS = 200


RISKY_HINTS = ("logs", "outputs", "checkpoints", "datasets", "wandb", "runs", "videos", "media")

PATTERNS = (
    (re.compile(r"(^|[;&|]\s*)ls\s+-R(\s|$)"), "Recursive directory listings can waste a lot of context."),
    (re.compile(r"(^|[;&|]\s*)find\s+(\.|\S+)(\s|$)"), "Broad find output can waste a lot of context; prefer limited depth or safe_find.py."),
    (re.compile(r"(^|[;&|]\s*)(grep\s+-R|grep\s+--recursive)(\s|$)"), "Recursive grep is usually too broad; prefer rg with path filters."),
    (re.compile(r"(^|[;&|]\s*)git\s+diff(\s|$)"), "Full git diff is expensive; prefer git diff --stat first."),
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
                return value
    if isinstance(tool_input, str):
        return tool_input
    return ""


def _warnings(command: str) -> list[str]:
    warnings: list[str] = []
    stripped = command.strip()
    for pattern, message in PATTERNS:
        if pattern.search(stripped):
            if stripped.startswith("git diff") and ("--stat" in stripped or "--name-only" in stripped):
                continue
            warnings.append(message)
    if stripped.startswith("cat ") and any(f"/{hint}/" in f"/{stripped.lower()}/" for hint in RISKY_HINTS):
        warnings.append("Direct reads from generated artifacts are expensive; summarize first unless raw output is required.")
    if stripped.startswith(("rg ", "grep ")) and (" ." in stripped or " ./" in stripped):
        warnings.append("Search scope looks broad; narrow the path set or use safe_rg.py.")
    return warnings


def _advisory_state_path() -> Path:
    return Path(sys.argv[0]).absolute().parent / ADVISORY_STATE_FILENAME


def _read_advisory_state() -> dict[str, list[str]]:
    try:
        data = json.loads(_advisory_state_path().read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(session): [str(item) for item in messages if isinstance(item, str)]
        for session, messages in data.items()
        if isinstance(messages, list)
    }


def _write_advisory_state(state: dict[str, list[str]]) -> None:
    state = dict(list(state.items())[-MAX_ADVISORY_SESSIONS:])
    path = _advisory_state_path()
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(state))
        os.replace(temporary, path)
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass


def _first_advisory(session_id: str, message: str) -> bool:
    state = _read_advisory_state()
    digest = hashlib.sha256(message.encode()).hexdigest()
    seen = state.get(session_id, [])
    if digest in seen:
        return False
    seen.append(digest)
    state.pop(session_id, None)
    state[session_id] = seen[-200:]
    _write_advisory_state(state)
    return True


def main() -> int:
    payload = _load_payload()
    if str(payload.get("tool_name", "")) != "Bash":
        return 0
    command = _tool_command(payload)
    if not command:
        return 0
    warnings = _warnings(command)
    if not warnings:
        return 0
    message = "Token risk: " + " ".join(warnings)
    if not _first_advisory(str(payload.get("session_id") or "unknown-session"), message):
        return 0
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": message,
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
