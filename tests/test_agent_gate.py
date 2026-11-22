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
