import json
import os
import subprocess
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "tools" / "hooks" / "pre_tool_use_policy.py"


def _run(cwd, command, env=None):
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "session_id": "t", "cwd": str(cwd)}
    e = {**os.environ, **(env or {})}
    e.pop("CDX_ENGINE", None) if not env or "CDX_ENGINE" not in env else None
    out = subprocess.run(["/usr/bin/python3", str(HOOK)], input=json.dumps(payload), capture_output=True, text=True,
                         env=e, timeout=30).stdout
    return json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out.strip() else None


def test_hardware_pattern_asks(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".cdx").mkdir()
    (tmp_path / ".cdx" / "hardware.json").write_text(json.dumps({"patterns": [r"\bur_driver\b", r"gripper_ctl"]}))
    assert _run(tmp_path, "python -m ur_driver --move home") == "ask"
    assert _run(tmp_path, "python -m ur_driver --move home", {"CDX_ENGINE": "codex"}) == "deny"
    assert _run(tmp_path, "python sim_only.py") is None
