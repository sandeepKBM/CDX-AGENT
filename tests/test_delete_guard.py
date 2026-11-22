import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[1] / "tools" / "hooks"
spec = importlib.util.spec_from_file_location("delete_guard", HOOKS / "delete_guard.py")
dg = importlib.util.module_from_spec(spec)
sys.modules["delete_guard"] = dg
spec.loader.exec_module(dg)


@pytest.mark.parametrize("cmd, paths", [
    ("rm -rf build", ["build"]),
    ("rm -fr build", ["build"]),
    ("rm -r -f build other", ["build", "other"]),
    ("sudo rm -rf /x", ["/x"]),
    ("cd a && rm -rf b", ["b"]),
    ("FOO=1 timeout 5 rm -- -weird", ["-weird"]),
    ("rmdir empty", ["empty"]),
    ("unlink f", ["f"]),
    ("find out -name '*.pyc' -delete", ["out"]),
    ("find . -type d -exec rm -rf {} +", ["."]),
    ("bash -c 'rm -rf tmpdir'", ["tmpdir"]),
    ("echo start\nrm -rf x\necho done", ["x"]),
])
def test_parse_delete_targets(cmd, paths):
    assert dg.parse(cmd).paths == paths


@pytest.mark.parametrize("cmd", [
    "ls | xargs rm -rf",
    "ssh node1 \"rm -rf /tmp/x\"",
    "python -c 'import shutil; shutil.rmtree(\"x\")'",
    "git clean -fd",
    "git clean -xfd",
    "echo $(rm -rf x)",
])
def test_parse_unparsable(cmd):
    assert dg.parse(cmd).unparsable


@pytest.mark.parametrize("cmd", [
    "echo 'rm -rf x'",
    "git clean -n -d",
    "grep -rn 'rm -rf' docs",
    "cat <<'EOF' > s.sh\nrm -rf x\nEOF",
    "ls -la",
    "python -m pytest tests",
])
def test_parse_no_delete(cmd):
    p = dg.parse(cmd)
    assert not p.paths and not p.unparsable and not p.git_worktree


def test_git_worktree_remove_target():
    assert dg.parse("git worktree remove --force .claude/worktrees/a").git_worktree == [".claude/worktrees/a"]


def test_expand_rejects_variables_and_empty_globs(tmp_path):
    assert dg._expand("$HOME/x", tmp_path) is None
    assert dg._expand("~other/x", tmp_path) is None
    assert dg._expand("nomatch*", tmp_path) is None
    (tmp_path / "a1").write_text("")
    assert dg._expand("a*", tmp_path) == [tmp_path / "a1"]


def test_unparsable_denied_and_audited(tmp_path, monkeypatch):
    monkeypatch.setenv("CDX_AUDIT_DIR", str(tmp_path / "audit"))
    v = dg.check("ls | xargs rm -rf", str(tmp_path))
    assert v.action == "deny" and "cdx-agent --rm" in v.reason
    rows = [json.loads(x) for f in (tmp_path / "audit").iterdir() for x in f.read_text().splitlines()]
    assert rows and rows[0]["action"] == "denied"


def test_classifier_unavailable_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("CDX_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(dg, "_load_policy_module", lambda: (_ for _ in ()).throw(ImportError("x")))
    (tmp_path / "d").mkdir()
    v = dg.check("rm -rf d", str(tmp_path))
    assert v.action == "deny" and "unavailable" in v.reason


def test_no_delete_no_verdict(tmp_path):
    assert dg.check("ls -la", str(tmp_path)) is None


def _hook(payload, env_extra):
    env = dict(os.environ, **env_extra)
    return subprocess.run([sys.executable, str(HOOKS / "pre_tool_use_policy.py")], input=json.dumps(payload),
                          capture_output=True, text=True, env=env, timeout=60)


def test_hook_denies_unparsable_delete(tmp_path):
    p = _hook({"tool_name": "Bash", "tool_input": {"command": "ls | xargs rm -rf"}, "cwd": str(tmp_path),
               "session_id": "t"}, {"CDX_AUDIT_DIR": str(tmp_path / "audit")})
    out = json.loads(p.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hook_git_reset_hard_only_when_executed(tmp_path):
    env = {"CDX_AUDIT_DIR": str(tmp_path / "audit")}
    p = _hook({"tool_name": "Bash", "tool_input": {"command": "echo 'never git reset --hard here'"},
               "cwd": str(tmp_path), "session_id": "t"}, env)
    assert "deny" not in p.stdout
    p = _hook({"tool_name": "Bash", "tool_input": {"command": "git reset --hard HEAD"}, "cwd": str(tmp_path),
               "session_id": "t"}, env)
    assert json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.fixture
def real_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("CDX_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("CDX_DELETE_POLICY", str(Path(__file__).resolve().parents[1] / "tools/templates/delete_policy.json"))
    monkeypatch.setenv("CDX_AGENT_PYTHONPATH", str(Path(__file__).resolve().parents[1] / "src"))
    monkeypatch.setenv("CDX_TRASH_ROOTS", json.dumps([str(tmp_path / "trash")]))
    work = tmp_path / "work"
    work.mkdir()
    return work


def test_tier_a_pycache_allowed_and_logged(real_policy, tmp_path):
    (real_policy / "__pycache__").mkdir()
    v = dg.check("rm -rf __pycache__", str(real_policy))
    assert v.action == "allow"
    rows = [json.loads(x) for f in (tmp_path / "audit").iterdir() for x in f.read_text().splitlines()]
    assert rows[-1]["action"] == "allowed" and rows[-1]["tier"] == "a"


def test_tier_b_plain_folder_denied_with_rm_hint(real_policy):
    (real_policy / "build").mkdir()
    v = dg.check("rm -r build", str(real_policy))
    assert v.action == "deny" and "cdx-agent --rm" in v.reason and "tier b" in v.reason


@pytest.mark.parametrize("make, cmd", [
    (lambda w: (w / "checkpoints" / "run1").mkdir(parents=True), "rm -rf checkpoints"),
    (lambda w: (w / "model.pt").write_text("x"), "rm model.pt"),
    (lambda w: (w / "repo" / ".git").mkdir(parents=True), "rm -rf repo/.git"),
])
def test_tier_c_protected_denied_with_user_line(real_policy, make, cmd):
    make(real_policy)
    v = dg.check(cmd, str(real_policy))
    assert v.action == "deny" and "tier c" in v.reason and "! rm -r" in v.reason


def test_mixed_targets_strictest_wins(real_policy):
    (real_policy / "__pycache__").mkdir()
    (real_policy / "keep").mkdir()
    v = dg.check("rm -rf __pycache__ keep", str(real_policy))
    assert v.action == "deny" and "tier b" in v.reason


def test_own_session_scratch_is_tier_a(real_policy, tmp_path):
    scratch = tmp_path / "sess-123" / "scratchpad"
    (scratch / "junk").mkdir(parents=True)
    assert dg.check(f"rm -r {scratch}/junk", str(real_policy), session_id="sess-123").action == "allow"
    assert dg.check(f"rm -r {scratch}/junk", str(real_policy), session_id="other").action == "deny"
