#!/usr/bin/env python3
"""PreToolUse hook (Bash): tool output stays in the agent's context and is re-sent every turn, so it is capped.

1. A single bare pytest command (optional VAR=x prefixes, `timeout N`, `python -m pytest`; no pipe, redirect, `;`, `&`, subshell,
   backticks, `$(`, newline) is rewritten to
       set -o pipefail; ( <cmd> ) 2>&1 | tail -n 60
2. Every other foreground Bash command is wrapped (shell state such as `cd` is kept, quoting and heredocs are untouched):
       __cdx_t=$(mktemp); { <cmd>
       } > "$__cdx_t" 2>&1; __cdx_rc=$?; python3 output_filter.py --cap "$__cdx_t"; rm -f "$__cdx_t"; (exit $__cdx_rc)
   Output over CAP_LINES (80) lines or CAP_BYTES (8 KB) keeps the first 30 and last 30 lines plus the error/traceback/failed/
   exception/warning lines of the cut middle (deduplicated, max 40) and the note "[cdx: N lines cut; CDX_RAW=1 for full]".
   The exit status is preserved.
Not wrapped: CDX_RAW=1 anywhere in the command or env, run_in_background, a last pipeline stage of head/tail/wc/grep -c, commands
that background with `&`, or use exit/exec (they would end the shell before the filter runs). CDX_OUTPUT_FILTER=0 disables all.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys

TAIL = int(os.environ.get("CDX_OUTPUT_FILTER_TAIL", "60"))
PYTEST = re.compile(r"^\s*(?:[A-Z_][A-Z0-9_]*=\S+\s+)*(?:timeout\s+\d+[smh]?\s+)?(?:\S*python[\d.]*\s+-m\s+)?pytest(?:\s|$)")
UNSAFE = re.compile(r"[|;&<>`\n]|\$\(|\(|\)")
RAW = re.compile(r"(^|\s)CDX_RAW=1(\s|$)")
CAP_LINES, CAP_BYTES, KEEP, MAX_MATCH, MAX_LINE = 80, 8192, 30, 40, 500
MATCH = re.compile(r"error|traceback|failed|exception|warning", re.I)
SMALL_LAST_STAGE = re.compile(r"(?:\||^)\s*(?:head|tail|wc|grep\s+-\w*c\w*)\b[^|]*$")
LONE_AMP = re.compile(r"(?<![&>|\\])&(?![&>\d])")
SHELL_END = re.compile(r"(?<![\w.-])(?:exit|exec)(?![\w.-])")


def rewrite(cmd: str) -> str | None:
    if not PYTEST.match(cmd) or UNSAFE.search(cmd) or RAW.search(cmd):
        return None
    return f"set -o pipefail; ( {cmd.strip()} ) 2>&1 | tail -n {TAIL}"


def cap_text(text: str) -> str:
    """Cut a long output to head + matching middle lines + tail; short output is returned unchanged."""
    lines = text.splitlines()
    if len(lines) <= CAP_LINES and len(text.encode("utf-8", "replace")) <= CAP_BYTES:
        return text

    def clip(line: str) -> str:
        return line if len(line) <= MAX_LINE else line[:MAX_LINE] + f" ...[{len(line) - MAX_LINE} chars cut]"

    if len(lines) <= 2 * KEEP:          # few but very long lines
        return "\n".join(clip(x) for x in lines) + f"\n[cdx: long lines clipped to {MAX_LINE} chars; CDX_RAW=1 for full]\n"
    head, mid, tail = lines[:KEEP], lines[KEEP:-KEEP], lines[-KEEP:]
    seen, hits = set(), []
    for line in mid:
        key = line.strip()
        if key and key not in seen and MATCH.search(line):
            seen.add(key)
            hits.append(line)
            if len(hits) >= MAX_MATCH:
                break
    cut = len(mid) - len(hits)
    out = head + [f"[cdx: {cut} lines cut; CDX_RAW=1 for full]"] + hits + tail
    return "\n".join(clip(x) for x in out) + "\n"


def wrap_cap(cmd: str, script: str | None = None) -> str | None:
    """Wrap an arbitrary command so its combined output is capped; None = leave the command alone."""
    c = cmd.strip()
    if not c or PYTEST.match(c) or RAW.search(c) or SMALL_LAST_STAGE.search(c) or SHELL_END.search(c):
        return None
    if LONE_AMP.search(c) or re.search(r"\b(?:nohup|setsid|disown)\b", c) or "__cdx_t" in c:
        return None
    script = script or os.path.abspath(__file__)
    py = shlex.quote(sys.executable or "/usr/bin/python3")
    s = shlex.quote(script)
    return (f'__cdx_t=$(mktemp); {{ {cmd.strip()}\n}} > "$__cdx_t" 2>&1; __cdx_rc=$?; '
            f'{py} {s} --cap "$__cdx_t" || cat "$__cdx_t"; rm -f "$__cdx_t"; (exit $__cdx_rc)')


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--cap":
        with open(sys.argv[2], "rb") as f:
            sys.stdout.write(cap_text(f.read().decode("utf-8", "replace")))
        return 0
    if os.environ.get("CDX_OUTPUT_FILTER", "1") == "0" or os.environ.get("CDX_RAW") == "1":
        return 0
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    ti = payload.get("tool_input") or {}
    if payload.get("tool_name") != "Bash" or ti.get("run_in_background"):
        return 0
    cmd = str(ti.get("command") or "")
    new, why = rewrite(cmd), f"pytest output trimmed to the last {TAIL} lines"
    if new is None:
        new, why = wrap_cap(cmd), f"output capped to {CAP_LINES} lines / {CAP_BYTES} bytes"
    if new is None:
        return 0
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                             "permissionDecisionReason": why,
                                             "updatedInput": {**ti, "command": new}}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
