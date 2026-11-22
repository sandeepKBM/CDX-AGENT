import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "tools" / "hooks" / "pre_tool_use_policy.py"


def _git(*args, cwd):
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": os.environ["PATH"], "HOME": str(cwd)}
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=env)


def _decide(script, command, cwd):
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "session_id": "s", "cwd": str(cwd)}
    out = subprocess.run([sys.executable, str(script)], input=json.dumps(payload), text=True,
                         capture_output=True, timeout=30).stdout
    return json.loads(out)["hookSpecificOutput"].get("permissionDecision") if out.strip() else None


def _setup(tmp_path):
    script = tmp_path / "hook.py"
    shutil.copy(HOOK, script)
    main = tmp_path / "main"
    main.mkdir()
    _git("init", "-q", cwd=main)
    (main / ".cdx").mkdir()
    (main / ".cdx" / "long_suites.txt").write_text("tests/test_local.py local-only\ntests/test_slow.py\n")
    (main / "tests").mkdir()
    for name in ("test_local.py", "test_slow.py"):
        (main / "tests" / name).write_text("def test_x(): pass\n")
    (main / "README").write_text("x")
    _git("add", "README", "tests", cwd=main)
    _git("commit", "-q", "-m", "init", cwd=main)
    return script, main


def test_worktree_reads_main_checkout_long_suites(tmp_path):
    script, main = _setup(tmp_path)
    wt = tmp_path / "wt"
    _git("worktree", "add", "-q", "-b", "b", str(wt), cwd=main)
    assert not (wt / ".cdx").exists()   # .cdx is untracked, so the worktree has none of its own
    assert _decide(script, "pytest -q tests/test_slow.py", wt) == "deny"


def test_ssh_and_srun_local_only_denied_plain_long_allowed(tmp_path):
    script, main = _setup(tmp_path)
    assert _decide(script, 'ssh node1 "cd repo && pytest -q tests/test_local.py"', main) == "deny"
    assert _decide(script, "srun -c 2 python -m pytest tests/test_local.py", main) == "deny"
    assert _decide(script, "ssh node1 pytest -q tests/test_slow.py", main) is None
    assert _decide(script, "ssh node1 pytest -q tests/test_other.py", main) is None
    assert _decide(script, "CDX_ALLOW_LONG_SUITE=1 ssh node1 pytest tests/test_local.py", main) is None
