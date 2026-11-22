"""agent_model_gate: subagent_type restrictions, concurrency cap, codex mapping; token_risk_warn is a no-op."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[1] / "tools" / "hooks"
POLICY = {"types": {
    "search": {"allowed": ["haiku"], "default": "haiku", "use_for": "lookups"},
    "safety-review": {"allowed": ["opus"], "default": "opus", "use_for": "safety"},
    "run": {"allowed": ["haiku"], "default": "haiku", "use_for": "runs", "subagent_types": ["runner", "general-purpose"]},
    "wait": {"allowed": [], "default": "", "use_for": "never"},
}}


def _gate(tmp_path, payload, env=None):
    hooks = tmp_path / "hooks"
    hooks.mkdir(exist_ok=True)
    script = hooks / "agent_model_gate.py"
    shutil.copy(HOOKS / "agent_model_gate.py", script)
    pol = tmp_path / "policy.json"
    pol.write_text(json.dumps(POLICY))
    e = {**os.environ, "AGENT_POLICY_FILE": str(pol), "AGENT_GATE_LOG": str(tmp_path / "log.jsonl"), **(env or {})}
    if not (env or {}).get("CDX_ENGINE"):
        e.pop("CDX_ENGINE", None)
    r = subprocess.run([sys.executable, str(script)], input=json.dumps(payload), text=True, capture_output=True, timeout=30, env=e)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)["hookSpecificOutput"] if r.stdout.strip() else None


def _payload(t, model, sub, n=0, session="s1"):
    return {"tool_name": "Agent", "session_id": session, "tool_use_id": f"u{n}", "tool_input": {
        "prompt": f"AGENT: type={t} model={model} why=a long enough justification here", "model": model,
        "subagent_type": sub, "description": f"d{n}"}}


def _dec(o):
    return o["permissionDecision"] if o else None


def test_search_general_purpose_denied_with_allowed_list(tmp_path):
    o = _gate(tmp_path, _payload("search", "haiku", "general-purpose"))
    assert _dec(o) == "deny" and "Explore" in o["permissionDecisionReason"] and "web-fetch" in o["permissionDecisionReason"]


def test_search_explore_ok(tmp_path):
    assert _dec(_gate(tmp_path, _payload("search", "haiku", "Explore"))) in (None, "allow")


def test_safety_review_hyphen_type_opus_ok_any_subagent(tmp_path):
    assert _dec(_gate(tmp_path, _payload("safety-review", "opus", "general-purpose"))) in (None, "allow")


def test_policy_subagent_types_key_overrides_default(tmp_path):
    assert _dec(_gate(tmp_path, _payload("run", "haiku", "general-purpose"))) in (None, "allow")
    assert _dec(_gate(tmp_path, _payload("run", "haiku", "Explore", n=1))) == "deny"


def test_fifth_concurrent_asks_and_codex_denies(tmp_path):
    for n in range(4):
        assert _dec(_gate(tmp_path, _payload("search", "haiku", "Explore", n))) in (None, "allow")
    assert _dec(_gate(tmp_path, _payload("search", "haiku", "Explore", 4))) == "ask"
    assert _dec(_gate(tmp_path, _payload("search", "haiku", "Explore", 5), {"CDX_ENGINE": "codex"})) == "deny"
    assert _dec(_gate(tmp_path, _payload("search", "haiku", "Explore", 6, session="other"))) in (None, "allow")   # per session


def test_old_entries_expire(tmp_path):
    (tmp_path / "hooks").mkdir()
    (tmp_path / "hooks" / ".agent_gate_state.json").write_text(json.dumps(
        {"s1": {f"old{i}": {"desc": "x", "started": 1.0} for i in range(6)}}))
    assert _dec(_gate(tmp_path, _payload("search", "haiku", "Explore"))) in (None, "allow")


def test_token_risk_warn_is_silent_noop():
    script = HOOKS / "token_risk_warn.py"
    payload = {"tool_name": "Bash", "tool_input": {"command": "ls -R ."}, "session_id": "a"}
    for stdin in (json.dumps(payload), "not json", ""):
        r = subprocess.run([sys.executable, str(script)], input=stdin, text=True, capture_output=True, timeout=30)
        assert r.returncode == 0 and r.stdout == ""


def _stop(tmp_path, session="s1", tid=None):
    """A SubagentStop event through subagent_claims.py (which calls the gate's release())."""
    hooks = tmp_path / "hooks"
    hooks.mkdir(exist_ok=True)
    for f in ("agent_model_gate.py", "subagent_claims.py"):
        shutil.copy(HOOKS / f, hooks / f)
    payload = {"hook_event_name": "SubagentStop", "session_id": session, "agent_id": "a1", "cwd": str(tmp_path),
               "last_assistant_message": "done"}
    if tid:
        payload["tool_use_id"] = tid
    e = {**os.environ, "CDX_CLAIMS_DIR": str(tmp_path / "claims")}
    r = subprocess.run([sys.executable, str(hooks / "subagent_claims.py")], input=json.dumps(payload), text=True,
                       capture_output=True, timeout=30, env=e)
    assert r.returncode == 0, r.stderr


def _count(tmp_path, session="s1"):
    f = tmp_path / "hooks" / ".agent_gate_state.json"
    return len(json.loads(f.read_text()).get(session, {})) if f.exists() else 0


def test_stop_event_clears_the_count(tmp_path):
    _gate(tmp_path, _payload("search", "haiku", "Explore", 0))
    assert _count(tmp_path) == 1
    _stop(tmp_path)
    assert _count(tmp_path) == 0
    _stop(tmp_path)   # a stop with nothing tracked is a no-op
    assert _count(tmp_path) == 0


def test_four_finished_plus_one_new_does_not_ask(tmp_path):
    for n in range(4):
        _gate(tmp_path, _payload("search", "haiku", "Explore", n))
    for _ in range(4):
        _stop(tmp_path)
    assert _dec(_gate(tmp_path, _payload("search", "haiku", "Explore", 9))) in (None, "allow")
    assert _count(tmp_path) == 1


def test_stop_matches_tool_use_id_else_oldest(tmp_path):
    for n in range(3):
        _gate(tmp_path, _payload("search", "haiku", "Explore", n))
    _stop(tmp_path, tid="u1")
    st = json.loads((tmp_path / "hooks" / ".agent_gate_state.json").read_text())["s1"]
    assert sorted(st) == ["u0", "u2"]
    _stop(tmp_path)   # no id: oldest goes
    st = json.loads((tmp_path / "hooks" / ".agent_gate_state.json").read_text())["s1"]
    assert sorted(st) == ["u2"]


def test_expiry_backstop_is_two_hours(tmp_path):
    import time
    _gate(tmp_path, _payload("search", "haiku", "Explore", 0))
    f = tmp_path / "hooks" / ".agent_gate_state.json"
    st = json.loads(f.read_text())
    st["s1"]["u0"]["started"] = time.time() - 2 * 3600 - 60
    f.write_text(json.dumps(st))
    _gate(tmp_path, _payload("search", "haiku", "Explore", 1))
    assert sorted(json.loads(f.read_text())["s1"]) == ["u1"]
