#!/usr/bin/env python3
"""SubagentStop hook: record every subagent's final claim as "unchecked" (WP10, F16; claim-audit skill).

One JSONL line per stop in <CDX_CLAIMS_DIR or /common/users/ss5772/.cdx/claims>/<project>.jsonl:
  {kind: "claim", time, host, session_id, agent_id, agent_type, project, cwd, agent_transcript, claim}
The owner re-runs the check and records it with `cdx-agent --verify-log add AGENT_ID --result ...`;
`cdx-agent --verify-log list --unchecked` shows claims nobody re-checked. Never blocks the stop, never prints.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

MAX_CLAIM = 6000


def _project(cwd: str) -> str:
    try:
        common = subprocess.run(["git", "-C", cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                capture_output=True, text=True, timeout=3).stdout.strip()
        if common:
            return Path(common).parent.name
    except (OSError, subprocess.SubprocessError):
        pass
    return Path(cwd).name or "unknown"


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if payload.get("hook_event_name") not in (None, "SubagentStop"):
        return 0
    cwd = str(payload.get("cwd") or os.getcwd())
    project = _project(cwd)
    claim = str(payload.get("last_assistant_message") or "")
    row = {
        "kind": "claim",
        "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "host": socket.gethostname().split(".")[0],
        "session_id": payload.get("session_id"),
        "agent_id": payload.get("agent_id"),
        "agent_type": payload.get("agent_type"),
        "project": project,
        "cwd": cwd,
        "agent_transcript": payload.get("agent_transcript_path"),
        "claim": claim[:MAX_CLAIM],
        "claim_truncated": len(claim) > MAX_CLAIM,
    }
    d = Path(os.environ.get("CDX_CLAIMS_DIR", "/common/users/ss5772/.cdx/claims"))
    d.mkdir(parents=True, exist_ok=True)
    with open(d / f"{project}.jsonl", "a") as f:
        f.write(json.dumps(row) + "\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        raise SystemExit(0)
