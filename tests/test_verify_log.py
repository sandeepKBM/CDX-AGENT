import json
import os
import subprocess
import sys
from pathlib import Path

from cdx_agent import verify_log

HOOK = Path(__file__).resolve().parents[1] / "tools" / "hooks" / "subagent_claims.py"


def test_subagent_stop_writes_claim_then_check(tmp_path, monkeypatch):
    monkeypatch.setenv("CDX_CLAIMS_DIR", str(tmp_path))
    payload = {"hook_event_name": "SubagentStop", "session_id": "s", "agent_id": "a1", "agent_type": "Explore",
               "cwd": str(tmp_path), "last_assistant_message": "Found 3 callers of foo()"}
    p = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True, text=True,
                       env=dict(os.environ, CDX_CLAIMS_DIR=str(tmp_path)))
    assert p.returncode == 0 and p.stdout == ""
    project = tmp_path.name
    rows = verify_log.joined(project)
    assert rows[0]["status"] == "agent-reported" and rows[0]["claim"].startswith("Found 3")
    verify_log.add(project, "a1", "refuted", "grep -rn 'foo(' src | wc -l")
    assert verify_log.joined(project)[0]["status"] == "refuted"
    assert verify_log.main(["list", "--project", project, "--unchecked"]) == 0


def test_add_requires_command(tmp_path, monkeypatch):
    monkeypatch.setenv("CDX_CLAIMS_DIR", str(tmp_path))
    assert verify_log.main(["add", "a1", "--result", "confirmed", "--command", " ", "--project", "p"]) == 2


def test_hook_ignores_bad_stdin(tmp_path):
    p = subprocess.run([sys.executable, str(HOOK)], input="nope", capture_output=True, text=True,
                       env=dict(os.environ, CDX_CLAIMS_DIR=str(tmp_path)))
    assert p.returncode == 0 and p.stdout == ""
