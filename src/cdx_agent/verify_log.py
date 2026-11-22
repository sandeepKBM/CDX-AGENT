"""cdx-agent --verify-log: subagent claims and the owner's re-checks (WP10, claim-audit skill).

    python -m cdx_agent.verify_log list [--project P] [--unchecked] [--json] [--limit N]
    python -m cdx_agent.verify_log add AGENT_ID --result confirmed|refuted|partial|not-checked \
        --command "CMD that re-ran the check" [--note TEXT] [--project P]

Claims are written by the SubagentStop hook (tools/hooks/subagent_claims.py) into
<CDX_CLAIMS_DIR or /common/users/ss5772/.cdx/claims>/<project>.jsonl. A claim stays "agent-reported" until a
check row with the same agent_id exists. Default project: the git main checkout name of the cwd.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Optional, Sequence

RESULTS = ("confirmed", "refuted", "partial", "not-checked")


def claims_dir() -> Path:
    return Path(os.environ.get("CDX_CLAIMS_DIR", "/common/users/ss5772/.cdx/claims"))


def default_project(cwd: Optional[str] = None) -> str:
    cwd = cwd or os.getcwd()
    try:
        common = subprocess.run(["git", "-C", cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                capture_output=True, text=True, timeout=5).stdout.strip()
        if common:
            return Path(common).parent.name
    except (OSError, subprocess.SubprocessError):
        pass
    return Path(cwd).name


def read_rows(project: str) -> list[dict]:
    f = claims_dir() / f"{project}.jsonl"
    rows = []
    try:
        for line in f.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except FileNotFoundError:
        pass
    return rows


def joined(project: str) -> list[dict]:
    rows = read_rows(project)
    checks: dict[str, list[dict]] = {}
    for r in rows:
        if r.get("kind") == "check":
            checks.setdefault(str(r.get("agent_id")), []).append(r)
    out = []
    for r in rows:
        if r.get("kind") != "claim":
            continue
        c = checks.get(str(r.get("agent_id")), [])
        out.append({**r, "checks": c, "status": c[-1]["result"] if c else "agent-reported"})
    return out


def add(project: str, agent_id: str, result: str, command: str, note: str = "") -> dict:
    if result not in RESULTS:
        raise ValueError(f"result must be one of {RESULTS}")
    if not command.strip():
        raise ValueError("--command is required: the command that re-ran the check")
    row = {"kind": "check", "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "agent_id": agent_id, "result": result,
           "command": command, "note": note, "by": os.environ.get("CDX_AGENT_ID") or os.environ.get("USER", "")}
    d = claims_dir()
    d.mkdir(parents=True, exist_ok=True)
    with open(d / f"{project}.jsonl", "a") as f:
        f.write(json.dumps(row) + "\n")
    return row


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="cdx-agent --verify-log", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--project")
    ls.add_argument("--unchecked", action="store_true")
    ls.add_argument("--json", action="store_true")
    ls.add_argument("--limit", type=int, default=30)
    ad = sub.add_parser("add")
    ad.add_argument("agent_id")
    ad.add_argument("--result", required=True, choices=RESULTS)
    ad.add_argument("--command", required=True)
    ad.add_argument("--note", default="")
    ad.add_argument("--project")
    a = ap.parse_args(argv)
    project = a.project or default_project()
    if a.cmd == "add":
        try:
            add(project, a.agent_id, a.result, a.command, a.note)
        except ValueError as e:
            print(f"verify-log: {e}")
            return 2
        print(f"recorded {a.result} for {a.agent_id} ({project})")
        return 0
    rows = joined(project)
    if a.unchecked:
        rows = [r for r in rows if r["status"] == "agent-reported"]
    rows = rows[-a.limit:]
    if a.json:
        print(json.dumps(rows, indent=1))
        return 0
    if not rows:
        print(f"no claims for {project}")
    for r in rows:
        first = (r.get("claim") or "").strip().splitlines()[:1]
        print(f"{r.get('time', '')[:16]}  {r.get('agent_id')}  {r.get('agent_type') or '-':16s} {r['status']:15s} "
              f"{(first[0] if first else '')[:90]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
