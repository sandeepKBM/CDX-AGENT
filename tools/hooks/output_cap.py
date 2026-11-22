#!/usr/bin/env python3
"""PostToolUse hook (Bash): cap long tool output before it enters the agent's context.

Tool output is re-sent every turn, so a long output is replaced (via updatedToolOutput) by head lines + a marker +
error-looking lines from the cut middle + tail lines. The full output is saved to a log file named in the marker.
No permission decision is ever printed. Opt-outs: run_in_background, image output, CDX_OUTPUT_FILTER=0, CDX_RAW=1 in
the env or in the command text, and `cdx-agent ... --help|-h` commands. Limits come from the values ledger (output_cap.lines/bytes/head/tail), default 80/8192/30/30.
Standalone: stdlib only. Any failure exits 0 silently.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

DEFAULTS = {"lines": 80, "bytes": 8192, "head": 30, "tail": 30}
LEDGER = "/common/users/ss5772/.cdx/values.jsonl"
MAX_MATCH, MAX_LINE = 40, 400
MATCH = re.compile(
    r"error|exception|traceback|fail|fatal|denied|not found|no such file|assert|warning", re.I)
PYTEST = re.compile(
    r"^\s*(?:[A-Z_][A-Z0-9_]*=\S+\s+)*(?:timeout\s+\d+[smh]?\s+)?(?:uv\s+run\s+)?(?:\S*python[\d.]*\s+-m\s+)?pytest(?:\s|$)")
PYTEST_SUMMARY = re.compile(r"^(?:FAILED|ERROR)\b|^=+ .*(?:passed|failed|error|no tests ran).* =+$|^\d+ (?:passed|failed)")
RAW = re.compile(r"(^|\s)CDX_RAW=1(\s|$)")
# `cdx-agent ... --help` / `-h` prints its own manual; capping it hides the usage the caller asked for.
HELP = re.compile(r"(^|[\s;&|(])(?:\S*/)?cdx-agent\b[^|;&]*\s(?:--help|-h)(?:\s|$)")


def limits() -> dict:
    out = dict(DEFAULTS)
    path = os.environ.get("CDX_VALUES_FILE") or LEDGER
    latest: dict = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for raw in f:
                try:
                    row = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(row, dict) and "key" in row:
                    latest[row["key"]] = row.get("value")
    except OSError:
        pass
    for name in DEFAULTS:
        v = latest.get(f"output_cap.{name}")
        try:
            if v is not None and int(v) > 0:
                out[name] = int(v)
        except (TypeError, ValueError):
            pass
    return out


def clip(line: str) -> str:
    return line if len(line) <= MAX_LINE else line[:MAX_LINE] + f" ...[{len(line) - MAX_LINE} chars cut]"


def fits(text: str, lim: dict) -> bool:
    return len(text.splitlines()) <= lim["lines"] and len(text.encode("utf-8", "replace")) <= lim["bytes"]


def cap_text(text: str, lim: dict, note: str, pytest: bool = False) -> str:
    """Head + note + matching middle lines + tail."""
    lines = text.splitlines()
    head_n, tail_n = lim["head"], lim["tail"]
    if len(lines) <= head_n + tail_n:    # few but very long lines
        return "\n".join(clip(x) for x in lines) + f"\n[cdx: long lines clipped to {MAX_LINE} chars; CDX_RAW=1 to skip the cap]"
    head = [] if pytest else lines[:head_n]
    start = 0 if pytest else head_n
    mid, tail = lines[start:-tail_n], lines[-tail_n:]
    pat = PYTEST_SUMMARY if pytest else MATCH
    seen, hits = set(), []
    for line in mid:
        key = line.strip()
        if key and key not in seen and pat.search(line):
            seen.add(key)
            hits.append(line)
            if len(hits) >= MAX_MATCH:
                break
    cut = len(mid) - len(hits)
    return "\n".join(clip(x) for x in head + [note.format(n=cut)] + hits + tail)


def save_full(payload: dict, cmd: str, stdout: str, stderr: str) -> str:
    base = os.environ.get("CDX_BASH_OUT_DIR")
    if not base:
        runtime = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        base = os.path.join(runtime, "bash_out")
    d = os.path.join(base, str(payload.get("session_id") or "nosession"))
    os.makedirs(d, mode=0o750, exist_ok=True)
    path = os.path.join(d, f"{time.strftime('%Y%m%d-%H%M%S')}_{os.getpid()}.log")
    with open(path, "w", encoding="utf-8", errors="replace") as f:
        f.write(f"$ {cmd}\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}\n")
    return path


def run() -> int:
    if os.environ.get("CDX_OUTPUT_FILTER", "1") == "0" or os.environ.get("CDX_RAW") == "1":
        return 0
    payload = json.load(sys.stdin)
    ti = payload.get("tool_input") or {}
    resp = payload.get("tool_response")
    if payload.get("tool_name") != "Bash" or ti.get("run_in_background") or not isinstance(resp, dict):
        return 0
    cmd = str(ti.get("command") or "")
    if resp.get("isImage") or RAW.search(cmd) or HELP.search(cmd):
        return 0
    stdout, stderr = str(resp.get("stdout") or ""), str(resp.get("stderr") or "")
    lim = limits()
    if fits(stdout + stderr, lim):
        return 0
    path = save_full(payload, cmd, stdout, stderr)
    note = "[cdx: {n} lines cut; full output: " + path.replace("{", "{{").replace("}", "}}") + "; CDX_RAW=1 to skip the cap]"
    pytest = bool(PYTEST.match(cmd))
    new = dict(resp)
    over_out, over_err = not fits(stdout, lim), not fits(stderr, lim)
    if over_out or not over_err:         # combined output over the limit with neither stream alone: cap stdout
        new["stdout"] = cap_text(stdout, lim, note, pytest)
    if over_err:
        new["stderr"] = cap_text(stderr, lim, note)
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": new}}))
    return 0


def main() -> int:
    try:
        return run()
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(main())
