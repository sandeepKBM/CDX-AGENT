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
    assert context.startswith("worktree baseline: 1 untracked")
    assert "\n" not in context  # one summary line, not the full `git status`

    # Identical tree, same session: pure repeat, must be silent (this was the
    # every-Bash-call spam the fix removes).
    second = _run_hook(script, _bash_payload(repo, "touch something"))
    assert second.returncode == 0
    assert second.stdout.strip() == ""

    (repo / "more.txt").write_text("y\n")
    third = _run_hook(script, _bash_payload(repo, "touch something"))
    context3 = json.loads(third.stdout)["hookSpecificOutput"]["additionalContext"]
    assert context3.startswith("worktree changed: 2 untracked")


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
    assert "Token tips" in context

    # Once per session: a second noisy command in the same session is silent.
    again = _run_hook(script, _bash_payload(plain_dir, "pytest -q tests/"))
    assert again.returncode == 0
    assert again.stdout.strip() == ""


def test_post_review_summary_is_one_line_for_big_trees(tmp_path):
    script = _script_copy(tmp_path, "post_tool_use_review.py")
    repo = _git_repo(tmp_path)
    reports = repo / "reports"
    for i in range(40):
        (reports / f"run{i}").mkdir(parents=True)
        (reports / f"run{i}" / "r.md").write_text("x\n")
    (repo / "top.txt").write_text("x\n")
    proc = _run_hook(script, _bash_payload(repo, "make all"))
    context = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert context == "worktree baseline: 2 untracked (top: (root) 1, reports/ 1)"


def test_post_review_tolerates_malformed_stdin(tmp_path):
    script = _script_copy(tmp_path, "post_tool_use_review.py")
    proc = _run_hook(script, "not json at all")
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


# --- pre_tool_use_policy.py and token_risk_warn.py ----------------------------------


def _pre_payload(command: str, session_id: str = "sess-1", tool_name: str = "Bash") -> dict:
    return {"tool_name": tool_name, "tool_input": {"command": command}, "session_id": session_id}


def test_advisory_hooks_dedupe_messages_but_not_permission_decisions(tmp_path):
    pre = _script_copy(tmp_path, "pre_tool_use_policy.py")
    first = _run_hook(pre, _pre_payload("find .", session_id="a"))
    assert "Token risk:" in json.loads(first.stdout)["hookSpecificOutput"]["additionalContext"]
    assert _run_hook(pre, _pre_payload("find .", session_id="a")).stdout.strip() == ""
    assert _run_hook(pre, _pre_payload("find .", session_id="b")).stdout.strip() != ""

    deny = _pre_payload("rm -rf ./build", session_id="a")
    first_deny = json.loads(_run_hook(pre, deny).stdout)
    second_deny = json.loads(_run_hook(pre, deny).stdout)
    assert first_deny["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert second_deny["hookSpecificOutput"]["permissionDecision"] == "deny"

    risk = _script_copy(tmp_path, "token_risk_warn.py")
    assert _run_hook(risk, _pre_payload("ls -R .", session_id="a")).stdout.strip() != ""
    assert _run_hook(risk, _pre_payload("ls -R .", session_id="a")).stdout.strip() == ""


def test_advisory_state_corruption_is_tolerated(tmp_path):
    script = _script_copy(tmp_path, "token_risk_warn.py")
    (script.parent / ".risk_advisory_state.json").write_text("not json")
    proc = _run_hook(script, _pre_payload("find ."))
    assert proc.returncode == 0 and proc.stdout.strip()


def test_post_review_large_output_stats_and_dedupe(tmp_path):
    script = _script_copy(tmp_path, "post_tool_use_review.py")
    response = {"stdout": "o" * 5000, "stderr": "e" * 4001}
    payload = _bash_payload(tmp_path, "echo secret-command-text", output="")
    payload["tool_response"] = response
    first = _run_hook(script, payload)
    context = json.loads(first.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "Large output (9 KB)" in context
    assert "secret-command-text" not in (tmp_path / "hooks" / ".tool_output_stats.jsonl").read_text()
    second = _run_hook(script, payload)
    assert "Large output" not in second.stdout
    stats = (tmp_path / "hooks" / ".tool_output_stats.jsonl").read_text().splitlines()
    assert len(stats) == 2
    records = [json.loads(line) for line in stats]
    assert all(record["tool"] == "Bash" and record["chars"] == 9001 for record in records)
    assert all(set(record) == {"t", "session", "tool", "chars"} for record in records)


def test_post_review_stats_rotate(tmp_path):
    script = _script_copy(tmp_path, "post_tool_use_review.py")
    stats = script.parent / ".tool_output_stats.jsonl"
    stats.write_text("x" * (5 * 1024 * 1024 + 1))
    proc = _run_hook(script, _bash_payload(tmp_path, "true"))
    assert proc.returncode == 0
    assert (script.parent / ".tool_output_stats.jsonl.1").is_file()
    assert stats.is_file()
