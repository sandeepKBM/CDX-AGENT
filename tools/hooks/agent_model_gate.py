#!/usr/bin/env python3
"""PreToolUse hook for Claude Code's Agent (formerly Task) tool: every subagent launch names its task type, model and why.

Policy: codex_tools/base/agent_policy.json (task type -> allowed models, default). The first line of the Agent prompt must be
    AGENT: type=<type> model=<model> why=<one-line justification>
and the Agent tool's `model` parameter must equal <model> (without it the subagent inherits the parent's model, usually Opus).

Decisions (hookSpecificOutput.permissionDecision):
  deny  - header missing / unknown type / `model` parameter not set or different / why too short / type "wait" (never an agent)
  ask   - model outside the type's `allowed` (an override: the user approves or rejects in the permission prompt); any fork
  (none)- within policy: no decision, the normal permission flow continues
Reasoning effort (policy types[t].effort = {default, max}, order low<medium<high<xhigh<max): an Agent call without `effort`
gets the type default injected through hookSpecificOutput.updatedInput (needs permissionDecision "allow", so a launch that was
"no decision" becomes an explicit allow; "ask" keeps asking with the injected input); an `effort` above the type max is an
override and asks like a model override; an unknown value is denied.
Every launch is appended to ~/.claude/agent_launch_log.jsonl (machine log; AGENT_GATE_LOG overrides the path).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

POLICY = Path(os.environ.get("AGENT_POLICY_FILE", "/common/users/ss5772/codex_tools/base/agent_policy.json"))
LOG = Path(os.environ.get("AGENT_GATE_LOG", str(Path.home() / ".claude" / "agent_launch_log.jsonl")))
HEADER = re.compile(r"^\s*AGENT:\s*type=(?P<type>[\w-]+)\s+model=(?P<model>[\w.-]+)\s+why=(?P<why>.+?)\s*$", re.M)
MIN_WHY = 15
EFFORTS = ["low", "medium", "high", "xhigh", "max"]


def _out(decision: str, reason: str) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                             "permissionDecisionReason": reason}}))


def _table(pol: dict) -> str:
    rows = [f"  {t}: allowed {v['allowed'] or 'NONE'} (default {v['default']})"
            + (f", effort default {v['effort']['default']} max {v['effort']['max']}" if v.get("effort") else "") + f" - {v['use_for']}"
            for t, v in pol["types"].items()]
    return "\n".join(rows)


def _out_effort(decision: str, reason: str, updated: dict) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                             "permissionDecisionReason": reason, "updatedInput": updated}}))


def _log(rec: dict) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def decide(payload: dict, pol: dict) -> tuple[str | None, str, dict]:
    ti = payload.get("tool_input") or {}
    sub = str(ti.get("subagent_type") or "general-purpose")
    param = ti.get("model")
    prompt = str(ti.get("prompt") or "")
    m = HEADER.search("\n".join(prompt.splitlines()[:5]))
    info = dict(subagent_type=sub, model_param=param, description=ti.get("description"))
    if m:
        info.update(type=m["type"], model=m["model"], why=m["why"])
    if sub == "fork":
        return "ask", "Fork copies this whole session's context (rule: never fork; use a fresh agent with a brief). Approve only if intended.", info
    need = f"Agent prompt line 1 must be 'AGENT: type=<type> model=<model> why=<justification>' and the `model` parameter set to it.\nPolicy {POLICY}:\n{_table(pol)}"
    if not m:
        return "deny", "No AGENT header. " + need, info
    t, model, why = m["type"], m["model"], m["why"]
    types = pol["types"]
    if t not in types:
        return "deny", f"Unknown task type {t!r}. " + need, info
    if t == "wait" or not types[t]["allowed"]:
        return "deny", f"Type {t!r}: {types[t]['use_for']}", info
    if not param or str(param) != model:
        return "deny", (f"Set the Agent `model` parameter to {model!r} (got {param!r}); without it the agent inherits the parent's model."), info
    if len(why) < MIN_WHY:
        return "deny", f"why= too short ({len(why)} chars): say why this model fits this task.", info
    if model not in types[t]["allowed"]:
        return _effort(types[t], ti, info, "ask", (f"OVERRIDE: type {t!r} allows {types[t]['allowed']} (default {types[t]['default']}); "
                                                   f"requested {model!r}. why: {why}"))
    return _effort(types[t], ti, info, None, "within policy")


def _effort(spec: dict, ti: dict, info: dict, decision: str | None, reason: str) -> tuple[str | None, str, dict]:
    """Check/inject the Agent `effort` against the type's {default, max}; combines with an existing model decision."""
    eff = spec.get("effort")
    if not eff:
        return decision, reason, info
    order = EFFORTS
    given = ti.get("effort")
    info.update(effort_param=given)
    if given is None or given == "":
        info.update(effort=eff["default"], effort_injected=True)
        return decision, reason + f" [effort {eff['default']} injected]", info
    given = str(given)
    info.update(effort=given)
    if given not in order:
        return "deny", f"Unknown effort {given!r}; allowed {order}.", info
    if order.index(given) > order.index(eff["max"]):
        msg = f"OVERRIDE: effort {given!r} exceeds max {eff['max']!r} (default {eff['default']!r}) for this type."
        return "ask", (decision and reason + "; " + msg) or msg, info
    return decision, reason, info


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if payload.get("tool_name") not in ("Agent", "Task"):
        return 0
    try:
        pol = json.loads(POLICY.read_text())
    except (OSError, json.JSONDecodeError) as e:
        _out("ask", f"agent policy unreadable ({e}); approve this launch manually.")
        return 0
    decision, reason, info = decide(payload, pol)
    _log(dict(ts=time.strftime("%Y-%m-%dT%H:%M:%S%z"), session=payload.get("session_id"), cwd=payload.get("cwd"),
              decision=decision or "allow", reason=reason[:300], **info))
    if info.get("effort_injected") and decision != "deny":
        ti = payload.get("tool_input") or {}
        _out_effort(decision or "allow", reason, {**ti, "effort": info["effort"]})
    elif decision:
        _out(decision, reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
