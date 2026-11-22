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

# F22: test the repo copies (tools/hooks), never the live deployed files.
HOOKS_DIR = Path(os.environ.get("CDX_AGENT_HOOKS_DIR", str(Path(__file__).resolve().parents[1] / "tools" / "hooks")))

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


def _run_hook(script: Path, payload, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, **(env_extra or {})}
    env.pop("CDX_WORKTREE_NOTES", None) if not (env_extra or {}).get("CDX_WORKTREE_NOTES") else None
    stdin_text = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run(
        [sys.executable, str(script)],
        input=stdin_text,
        text=True,
        capture_output=True,
        timeout=30,
        env=env,
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


def test_stop_summary_disabled_emits_nothing_on_dirty_tree(tmp_path):
    # stop_summary.py is a no-op at the user's request (2026-08-13); it is removed in WP10.
    script = _script_copy(tmp_path, "stop_summary.py")
    repo = _git_repo(tmp_path)
    (repo / "dirty.txt").write_text("x\n")
    proc = _run_hook(script, _stop_payload(repo))
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


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


WT_ON = {"CDX_WORKTREE_NOTES": "1"}


def test_post_review_baseline_then_silent_until_tree_changes(tmp_path):
    script = _script_copy(tmp_path, "post_tool_use_review.py")
    repo = _git_repo(tmp_path)
    (repo / "dirty.txt").write_text("x\n")

    first = _run_hook(script, _bash_payload(repo, "touch something"), WT_ON)
    context = json.loads(first.stdout)["hookSpecificOutput"]["additionalContext"]
    assert context.startswith("worktree baseline: 1 untracked")
    assert "\n" not in context  # one summary line, not the full `git status`

    # Identical tree, same session: pure repeat, must be silent (this was the
    # every-Bash-call spam the fix removes).
    second = _run_hook(script, _bash_payload(repo, "touch something"), WT_ON)
    assert second.returncode == 0
    assert second.stdout.strip() == ""

    (repo / "more.txt").write_text("y\n")
    third = _run_hook(script, _bash_payload(repo, "touch something"), WT_ON)
    context3 = json.loads(third.stdout)["hookSpecificOutput"]["additionalContext"]
    assert context3.startswith("worktree changed: 2 untracked")


def test_post_review_worktree_notes_silent_by_default(tmp_path):
    script = _script_copy(tmp_path, "post_tool_use_review.py")
    repo = _git_repo(tmp_path)
    (repo / "dirty.txt").write_text("x\n")
    proc = _run_hook(script, _bash_payload(repo, "touch something"))
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


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
    proc = _run_hook(script, _bash_payload(repo, "make all"), WT_ON)
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
    assert _run_hook(risk, _pre_payload("ls -R .", session_id="a")).stdout.strip() == ""   # F15: token_risk_warn is a no-op


def test_advisory_state_corruption_is_tolerated(tmp_path):
    script = _script_copy(tmp_path, "pre_tool_use_policy.py")
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


# --- agent_model_gate.py: every Agent launch names type / model / why; overrides ask the user -------------------------------
def _agent_payload(prompt: str, model=None, sub=None, tool_name: str = "Agent") -> dict:
    ti = {"prompt": prompt, "description": "t"}
    if sub is None:   # F16: search/run types only accept their own subagent_types
        sub = "runner" if "type=run " in prompt else "Explore" if "type=search " in prompt else None
    if model is not None:
        ti["model"] = model
    if sub is not None:
        ti["subagent_type"] = sub
    return {"tool_name": tool_name, "tool_input": ti, "session_id": "s1", "cwd": "/tmp"}


def _gate_out(tmp_path, monkeypatch, payload):
    script = _script_copy(tmp_path, "agent_model_gate.py")
    monkeypatch.setenv("AGENT_GATE_LOG", str(tmp_path / "launch.jsonl"))
    monkeypatch.setenv("AGENT_POLICY_FILE", str(HOOKS_DIR.parent / "templates" / "agent_policy.json"))  # not the live file
    r = _run_hook(script, payload)
    assert r.returncode == 0, r.stderr
    out = r.stdout.strip()
    return json.loads(out)["hookSpecificOutput"] if out else None


def _gate(tmp_path, monkeypatch, payload):
    o = _gate_out(tmp_path, monkeypatch, payload)
    return o["permissionDecision"] if o else None


@pytest.mark.parametrize("prompt,model,sub,want", [
    ("AGENT: type=run model=haiku why=launch the queue from a written recipe\nbody", "haiku", None, "allow"),   # no effort -> default injected
    ("AGENT: type=design model=opus why=trade-offs between three planner designs", "opus", None, "allow"),
    ("AGENT: type=run model=opus why=launch the queue from a written recipe", "opus", None, "ask"),    # override -> user
    ("AGENT: type=debug model=sonnet why=find why task4 held all episodes", None, None, "deny"),      # model param unset
    ("AGENT: type=debug model=sonnet why=find why task4 held all episodes", "haiku", None, "deny"),   # param != header
    ("AGENT: type=wait model=haiku why=wait for the slurm job to finish", "haiku", None, "deny"),     # never an agent
    ("AGENT: type=nosuch model=haiku why=something long enough here", "haiku", None, "deny"),
    ("AGENT: type=run model=haiku why=short", "haiku", None, "deny"),
    ("no header at all", "sonnet", None, "deny"),
    ("anything", None, "fork", "ask"),
])
def test_agent_gate_decisions(tmp_path, monkeypatch, prompt, model, sub, want):
    assert _gate(tmp_path, monkeypatch, _agent_payload(prompt, model, sub)) == want


def test_agent_gate_ignores_other_tools_and_logs_agent_launches(tmp_path, monkeypatch):
    assert _gate(tmp_path, monkeypatch, {"tool_name": "Bash", "tool_input": {"command": "ls"}}) is None
    assert not (tmp_path / "launch.jsonl").exists()
    _gate(tmp_path, monkeypatch, _agent_payload("AGENT: type=search model=haiku why=find all users of task_gate", "haiku"))
    rec = json.loads((tmp_path / "launch.jsonl").read_text().splitlines()[-1])
    assert rec["decision"] == "allow" and rec["type"] == "search" and rec["model"] == "haiku"


def _with_effort(prompt, model, effort):
    p = _agent_payload(prompt, model)
    if effort is not None:
        p["tool_input"]["effort"] = effort
    return p


@pytest.mark.parametrize("t,model,effort,decision,injected", [
    ("search", "haiku", None, "allow", "low"),
    ("run", "haiku", None, "allow", "low"),
    ("implement", "sonnet", None, "allow", "medium"),
    ("review", "sonnet", None, "allow", "medium"),
    ("design", "opus", None, "allow", "high"),
    ("run", "haiku", "medium", None, None),            # explicit and within max: untouched, no decision
    ("implement", "sonnet", "high", None, None),
    ("design", "opus", "xhigh", None, None),
    ("run", "haiku", "high", "ask", None),             # above the type max -> user decides
    ("implement", "sonnet", "xhigh", "ask", None),
    ("design", "opus", "max", "ask", None),
    ("run", "haiku", "turbo", "deny", None),           # unknown value
])
def test_agent_gate_effort_policy(tmp_path, monkeypatch, t, model, effort, decision, injected):
    prompt = f"AGENT: type={t} model={model} why=a long enough justification here"
    o = _gate_out(tmp_path, monkeypatch, _with_effort(prompt, model, effort))
    assert (o["permissionDecision"] if o else None) == decision
    if injected:
        assert o["updatedInput"]["effort"] == injected and o["updatedInput"]["prompt"] == prompt and o["updatedInput"]["model"] == model
    else:
        assert not o or "updatedInput" not in o
    rec = json.loads((tmp_path / "launch.jsonl").read_text().splitlines()[-1])
    assert rec["effort"] == (injected or effort) and bool(rec.get("effort_injected")) == bool(injected)


def test_agent_gate_effort_with_model_override_keeps_asking_and_injects(tmp_path, monkeypatch):
    prompt = "AGENT: type=run model=opus why=launch the queue from a written recipe"
    o = _gate_out(tmp_path, monkeypatch, _with_effort(prompt, "opus", None))
    assert o["permissionDecision"] == "ask" and o["updatedInput"]["effort"] == "low" and "OVERRIDE" in o["permissionDecisionReason"]


def test_agent_gate_old_task_tool_name_is_gated(tmp_path, monkeypatch):
    assert _gate(tmp_path, monkeypatch, _agent_payload("no header", tool_name="Task")) == "deny"


# --- output_filter.py: retired PreToolUse hook; it must make no decision and print nothing ---------------------------------
@pytest.mark.parametrize("cmd", [
    "pytest -q tests/x.py",
    "seq 1 300",
    "CDX_RAW=1 pytest -q",
    "cat <<'EOF'\nx\nEOF",
])
def test_output_filter_is_silent(tmp_path, cmd):
    script = _script_copy(tmp_path, "output_filter.py")
    r = _run_hook(script, {"tool_name": "Bash", "tool_input": {"command": cmd}})
    assert r.returncode == 0 and r.stdout == ""
    assert _run_hook(script, "not json").stdout == ""


# --- pre_tool_use_policy.py poll guard: no foreground wait loops, no self-matching pgrep/pkill -f regexes ---------------------
@pytest.mark.parametrize("cmd,background,denied", [
    ('until ! pgrep -f "pytest.*test_v4_foundation" >/dev/null; do sleep 5; done; tail -5 x', False, True),   # self-match, looped 7 min
    ("while ssh rlab1 squeue -j 1 -h | grep -q .; do sleep 300; done", False, True),
    ("while ssh rlab1 squeue -j 1 -h | grep -q .; do sleep 300; done", True, False),                     # background watcher: free
    ('pgrep -af "[p]ytest.*foo"', False, False),
    ('pkill -f "serve.*tower"', False, True),
    ("for f in a b; do echo $f; done", False, False),
    ("timeout 900 python -m pytest -q tests", False, False),
    ("cat > w.sh <<'EOF'\nwhile alive; do sleep 300; done\nEOF\necho ok", False, False),   # writing a watcher script is not waiting
    ("P=1; timeout 590 tail --pid=$P -f /dev/null; cat x", False, True),   # 2026-10-17: an agent waited on its own bg pytest this way
    ("wait $pid", False, True),
    ("echo hi; wait 123", False, True),
    ('git commit -m "guard: tail on pid / wait on another process"', False, False),   # prose is not a wait
    ("tail -n 5 log", False, False),
])
def test_pre_policy_poll_guard(tmp_path, cmd, background, denied):
    script = _script_copy(tmp_path, "pre_tool_use_policy.py")
    r = _run_hook(script, {"tool_name": "Bash", "tool_input": {"command": cmd, "run_in_background": background}, "session_id": "s"})
    assert r.returncode == 0
    out = json.loads(r.stdout) if r.stdout.strip() else {}
    assert (out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny") == denied


@pytest.mark.parametrize("command,denied", [
    ('git commit -m "fix: guard blocks tail --pid and while sleep loops"', False),
    ('echo "until done; do sleep 5; done"', False),
    ("bash -c 'while true; do sleep 5; done'", True),
    ("ssh rlab1 'while squeue -j 1 -h | grep -q .; do sleep 60; done'", True),
    ("P=1; timeout 590 tail --pid=$P -f /dev/null", True),
])
def test_pre_policy_poll_guard_only_matches_executed_commands(tmp_path, command, denied):
    script = _script_copy(tmp_path, "pre_tool_use_policy.py")
    result = _run_hook(script, _pre_payload(command))
    assert result.returncode == 0
    output = json.loads(result.stdout) if result.stdout.strip() else {}
    assert (output.get("hookSpecificOutput", {}).get("permissionDecision") == "deny") == denied


def test_pre_policy_poll_guard_background_still_allows_waits(tmp_path):
    script = _script_copy(tmp_path, "pre_tool_use_policy.py")
    payload = _pre_payload("while true; do sleep 5; done")
    payload["tool_input"]["run_in_background"] = True
    assert _run_hook(script, payload).stdout.strip() == ""


def test_pre_policy_long_suite_guard(tmp_path):
    script = _script_copy(tmp_path, "pre_tool_use_policy.py")
    repo = _git_repo(tmp_path)
    (repo / ".cdx").mkdir()
    (repo / ".cdx" / "long_suites.txt").write_text("# slow suites\ntests/test_slow_*.py\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_slow_case.py").write_text("def test_slow(): pass\n")
    (repo / "tests" / "test_fast.py").write_text("def test_fast(): pass\n")

    base_payload = _pre_payload("pytest -q tests/test_slow_case.py")
    base_payload["cwd"] = str(repo)
    denied = _run_hook(script, base_payload)
    assert json.loads(denied.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "Long test suite tests/test_slow_case.py:" in denied.stdout

    all_payload = _pre_payload("pytest -q")
    all_payload["cwd"] = str(repo)
    assert json.loads(_run_hook(script, all_payload).stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"

    for command in (
        "cdx-agent --cpu-job --wait -- pytest -q tests/test_slow_case.py",
        "python -m cdx_agent.cpu_job -- pytest -q tests/test_slow_case.py",
        "CDX_ALLOW_LONG_SUITE=1 pytest -q tests/test_slow_case.py",
        "pytest -q tests/test_fast.py",
        "ssh rlab1 pytest -q tests/test_slow_case.py",
        "srun pytest -q tests/test_slow_case.py",
    ):
        payload = _pre_payload(command)
        payload["cwd"] = str(repo)
        assert _run_hook(script, payload).stdout.strip() == ""

    other_root = tmp_path / "other"
    other_root.mkdir()
    empty_repo = _git_repo(other_root)
    empty_payload = _pre_payload("pytest", session_id="other", tool_name="Bash")
    empty_payload["cwd"] = str(empty_repo)
    assert _run_hook(script, empty_payload).stdout.strip() == ""


def test_pre_policy_long_suite_local_only_tag_advises_local_background_command(tmp_path):
    script = _script_copy(tmp_path, "pre_tool_use_policy.py")
    repo = _git_repo(tmp_path)
    (repo / ".cdx").mkdir()
    (repo / ".cdx" / "long_suites.txt").write_text("# c\ntests/test_slow_*.py\ntests/test_local.py local-only  # needs /data/local\n")
    (repo / "tests").mkdir()
    for name in ("test_slow_a.py", "test_local.py"):
        (repo / "tests" / name).write_text("def test_x(): pass\n")

    def reason(cmd):
        payload = _pre_payload(cmd)
        payload["cwd"] = str(repo)
        out = _run_hook(script, payload).stdout
        return json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"] if out.strip() else None

    local = reason("pytest -q tests/test_local.py")
    assert "local-only" in local and "run_in_background" in local and "--cpu-job --wait" not in local
    for part in ("OMP_NUM_THREADS=2", "nice -n 10", "CUDA_VISIBLE_DEVICES=", "timeout 3600", "pytest -q tests/test_local.py",
                 "CDX_ALLOW_LONG_SUITE=1"):
        assert part in local
    assert "--cpu-job --wait" in reason("pytest -q tests/test_slow_a.py")                 # untagged: unchanged
    assert reason(local.split("`")[1]) is None                                           # the advised command passes the hook
    assert reason("pytest -q") is not None                                              # whole-suite run still caught


def test_pre_policy_load_advisory_is_env_driven_and_deduped(tmp_path, monkeypatch):
    script = _script_copy(tmp_path, "pre_tool_use_policy.py")
    monkeypatch.setenv("CDX_TEST_LOAD", "8")
    monkeypatch.setenv("CDX_TEST_CPUS", "4")
    payload = _pre_payload("python train.py", session_id="load-session")
    first = _run_hook(script, payload)
    assert "Machine overloaded (load 8 on 4 cores)" in first.stdout
    assert _run_hook(script, payload).stdout.strip() == ""


def test_post_review_restart_nudge_levels_and_missing_or_small_transcripts(tmp_path, monkeypatch):
    script = _script_copy(tmp_path, "post_tool_use_review.py")
    monkeypatch.setenv("CDX_RESTART_NUDGE_MB", "0.001")
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_bytes(b"x" * 1200)
    payload = _bash_payload(tmp_path, "true", session_id="transcript-session")
    payload["transcript_path"] = str(transcript)

    first = _run_hook(script, payload)
    first_context = json.loads(first.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "This session's transcript is ~" in first_context
    assert _run_hook(script, payload).stdout.strip() == ""

    transcript.write_bytes(b"x" * 2200)
    second = _run_hook(script, payload)
    assert "This session's transcript is ~" in json.loads(second.stdout)["hookSpecificOutput"]["additionalContext"]
    assert _run_hook(script, payload).stdout.strip() == ""

    small = tmp_path / "small-transcript.jsonl"
    small.write_bytes(b"x" * 10)
    small_payload = _bash_payload(tmp_path, "true", session_id="small-transcript-session")
    small_payload["transcript_path"] = str(small)
    assert _run_hook(script, small_payload).stdout.strip() == ""


def test_post_review_restart_nudge_uses_last_usage_tokens(tmp_path, monkeypatch):
    monkeypatch.setenv("CDX_RESTART_NUDGE_MB", "0.0001")
    script = _script_copy(tmp_path, "post_tool_use_review.py")
    transcript = tmp_path / "t.jsonl"
    usage = {"input_tokens": 5, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 20, "output_tokens": 9}
    transcript.write_text(json.dumps({"message": {"role": "assistant", "usage": usage}}) + "\n")
    payload = _bash_payload(tmp_path, "true", session_id="usage-session")
    payload["transcript_path"] = str(transcript)
    ctx = json.loads(_run_hook(script, payload).stdout)["hookSpecificOutput"]["additionalContext"]
    assert "(1025 tokens)" in ctx and "~1025" not in ctx
