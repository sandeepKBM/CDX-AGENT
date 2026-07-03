"""Behavioral tests for the deployed hook scripts (stop_summary.py,
post_tool_use_review.py) -- the scripts themselves live outside this repo in
`tools_root/hooks` (see hooks.py), so these tests locate them via
CDX_AGENT_HOOKS_DIR (same skip-if-absent precedent as
CDX_AGENT_HOOKS_EXAMPLE_JSON in test_hooks.py) and run each one as a real
subprocess with a synthetic stdin payload.

The headline regression pinned here: a Stop hook that emits context on every
Stop event force-continues every turn and loops until the harness's
consecutive-block cap ("A hook blocked the turn from ending 9 consecutive
times"). The fix is threefold -- honor `stop_hook_active`, stay silent when
there is nothing to say, and dedupe identical summaries per session.

Scripts are copied into tmp_path before running so the state files they
write land in tmp, never in the real deployed hooks dir.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS_DIR = Path(os.environ.get("CDX_AGENT_HOOKS_DIR", "/common/users/ss5772/codex_tools/hooks"))

pytestmark = pytest.mark.skipif(
    not HOOKS_DIR.is_dir(), reason=f"deployed hooks dir not available: {HOOKS_DIR}"
)


def _script_copy(tmp_path: Path, name: str) -> Path:
    src = HOOKS_DIR / name
    if not src.is_file():
        pytest.skip(f"{name} not present in {HOOKS_DIR}")
    dst_dir = tmp_path / "hooks"
    dst_dir.mkdir(exist_ok=True)
    dst = dst_dir / name
    shutil.copy(src, dst)
    return dst


def _run_hook(script: Path, payload) -> subprocess.CompletedProcess:
    stdin_text = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run(
        [sys.executable, str(script)],
        input=stdin_text,
        text=True,
        capture_output=True,
        timeout=30,
    )


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


def _stop_payload(cwd: Path, session_id: str = "sess-1", stop_hook_active: bool = False) -> dict:
    return {"cwd": str(cwd), "session_id": session_id, "stop_hook_active": stop_hook_active}


def _bash_payload(cwd: Path, command: str, session_id: str = "sess-1", output: str = "") -> dict:
    return {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_output": output,
        "cwd": str(cwd),
        "session_id": session_id,
    }


# --- stop_summary.py -----------------------------------------------------------------


def test_stop_summary_silent_when_stop_hook_active(tmp_path):
    # The loop-bug regression test: a stop that is itself a hook-forced
    # continuation must pass through with zero output, no matter how dirty
    # the tree is.
    script = _script_copy(tmp_path, "stop_summary.py")
    repo = _git_repo(tmp_path)
    (repo / "dirty.txt").write_text("x\n")
    proc = _run_hook(script, _stop_payload(repo, stop_hook_active=True))
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_stop_summary_silent_on_clean_tree(tmp_path):
    script = _script_copy(tmp_path, "stop_summary.py")
    repo = _git_repo(tmp_path)
    proc = _run_hook(script, _stop_payload(repo))
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_stop_summary_silent_outside_git(tmp_path):
    script = _script_copy(tmp_path, "stop_summary.py")
    plain_dir = tmp_path / "not_a_repo"
    plain_dir.mkdir()
    proc = _run_hook(script, _stop_payload(plain_dir))
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_stop_summary_emits_once_then_dedupes_same_session(tmp_path):
    script = _script_copy(tmp_path, "stop_summary.py")
    repo = _git_repo(tmp_path)
    (repo / "dirty.txt").write_text("x\n")

    first = _run_hook(script, _stop_payload(repo))
    assert first.returncode == 0
    emitted = json.loads(first.stdout)
    context = emitted["hookSpecificOutput"]["additionalContext"]
    assert emitted["hookSpecificOutput"]["hookEventName"] == "Stop"
    assert "dirty.txt" in context

    second = _run_hook(script, _stop_payload(repo))
    assert second.returncode == 0
    assert second.stdout.strip() == ""

    (repo / "more.txt").write_text("y\n")
    third = _run_hook(script, _stop_payload(repo))
    assert third.returncode == 0
    assert "more.txt" in json.loads(third.stdout)["hookSpecificOutput"]["additionalContext"]


def test_stop_summary_new_session_emits_even_if_tree_unchanged(tmp_path):
    script = _script_copy(tmp_path, "stop_summary.py")
    repo = _git_repo(tmp_path)
    (repo / "dirty.txt").write_text("x\n")
    assert _run_hook(script, _stop_payload(repo, session_id="a")).stdout.strip() != ""
    assert _run_hook(script, _stop_payload(repo, session_id="a")).stdout.strip() == ""
    assert _run_hook(script, _stop_payload(repo, session_id="b")).stdout.strip() != ""


def test_stop_summary_tolerates_malformed_stdin(tmp_path):
    script = _script_copy(tmp_path, "stop_summary.py")
    proc = _run_hook(script, "this is not json {")
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


# --- post_tool_use_review.py ----------------------------------------------------------


def test_post_review_silent_for_read_only_command(tmp_path):
    script = _script_copy(tmp_path, "post_tool_use_review.py")
    repo = _git_repo(tmp_path)
    (repo / "dirty.txt").write_text("x\n")
    proc = _run_hook(script, _bash_payload(repo, "ls -la"))
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_post_review_baseline_then_silent_until_tree_changes(tmp_path):
    script = _script_copy(tmp_path, "post_tool_use_review.py")
    repo = _git_repo(tmp_path)
    (repo / "dirty.txt").write_text("x\n")

    first = _run_hook(script, _bash_payload(repo, "touch something"))
    context = json.loads(first.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "session baseline" in context
    assert "dirty.txt" in context

    # Identical tree, same session: pure repeat, must be silent (this was the
    # every-Bash-call spam the fix removes).
    second = _run_hook(script, _bash_payload(repo, "touch something"))
    assert second.returncode == 0
    assert second.stdout.strip() == ""

    (repo / "more.txt").write_text("y\n")
    third = _run_hook(script, _bash_payload(repo, "touch something"))
    context3 = json.loads(third.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "changed after shell command" in context3
    assert "more.txt" in context3


def test_post_review_clean_tree_emits_nothing(tmp_path):
    script = _script_copy(tmp_path, "post_tool_use_review.py")
    repo = _git_repo(tmp_path)
    proc = _run_hook(script, _bash_payload(repo, "true"))
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_post_review_token_hints_fire_independently_of_git(tmp_path):
    script = _script_copy(tmp_path, "post_tool_use_review.py")
    plain_dir = tmp_path / "not_a_repo"
    plain_dir.mkdir()
    proc = _run_hook(script, _bash_payload(plain_dir, "pytest -q tests/"))
    context = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "Token-saving follow-ups" in context


def test_post_review_tolerates_malformed_stdin(tmp_path):
    script = _script_copy(tmp_path, "post_tool_use_review.py")
    proc = _run_hook(script, "not json at all")
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""
