"""output_cap.py (PostToolUse, Bash): long output is replaced via updatedToolOutput; the full text goes to a log file."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent / "tools" / "hooks"
CAP = HOOKS / "output_cap.py"


def _run(tmp_path, payload, env_extra=None, stdin=None):
    env = {k: v for k, v in os.environ.items() if k not in ("CDX_RAW", "CDX_OUTPUT_FILTER", "CDX_VALUES_FILE")}
    env["CDX_BASH_OUT_DIR"] = str(tmp_path / "out")
    env["CDX_VALUES_FILE"] = str(tmp_path / "no_such_values.jsonl")
    env.update(env_extra or {})
    return subprocess.run([sys.executable, str(CAP)], input=stdin if stdin is not None else json.dumps(payload),
                          text=True, capture_output=True, timeout=30, env=env)


def _payload(stdout, cmd="seq 1 500", stderr="", **resp):
    return {"tool_name": "Bash", "session_id": "s1", "tool_input": {"command": cmd},
            "tool_response": {"stdout": stdout, "stderr": stderr, "interrupted": False, "isImage": False, **resp}}


def _seq(n):
    return "\n".join(str(i) for i in range(1, n + 1)) + "\n"


def _reply(r):
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PostToolUse" and "permissionDecision" not in out
    return out["updatedToolOutput"]


def test_long_output_is_capped_and_saved(tmp_path):
    new = _reply(_run(tmp_path, _payload(_seq(500))))
    assert set(new) == {"stdout", "stderr", "interrupted", "isImage"}
    lines = new["stdout"].splitlines()
    assert lines[0] == "1" and lines[29] == "30" and lines[-1] == "500" and len(lines) <= 61
    marker = next(x for x in lines if x.startswith("[cdx: "))
    assert "440 lines cut" in marker and "CDX_RAW=1 to skip the cap" in marker
    path = Path(marker.split("full output: ")[1].split(";")[0])
    assert path.parent == tmp_path / "out" / "s1"
    text = path.read_text()
    assert text.startswith("$ seq 1 500\n--- stdout ---\n") and "--- stderr ---" in text
    assert all(str(i) in text.splitlines() for i in (1, 250, 500))


def test_error_lines_from_cut_middle_are_kept(tmp_path):
    out = _seq(200) + "FATAL: boom\nFATAL: boom\n" + _seq(200)
    new = _reply(_run(tmp_path, _payload(out)))
    assert new["stdout"].splitlines().count("FATAL: boom") == 1


def test_small_output_is_silent(tmp_path):
    r = _run(tmp_path, _payload(_seq(50)))
    assert r.returncode == 0 and r.stdout == ""
    assert not (tmp_path / "out").exists()


def test_opt_outs_are_silent(tmp_path):
    big = _seq(500)
    cases = [
        (_payload(big, isImage=True), None),
        ({**_payload(big), "tool_input": {"command": "seq 1 500", "run_in_background": True}}, None),
        (_payload(big, cmd="CDX_RAW=1 seq 1 500"), None),
        (_payload(big), {"CDX_RAW": "1"}),
        (_payload(big), {"CDX_OUTPUT_FILTER": "0"}),
        ({**_payload(big), "tool_name": "Read"}, None),
    ]
    for payload, env in cases:
        r = _run(tmp_path, payload, env)
        assert r.returncode == 0 and r.stdout == ""


def test_ledger_overrides_limits_latest_row_wins(tmp_path):
    ledger = tmp_path / "values.jsonl"
    ledger.write_text("\n".join([
        json.dumps({"key": "output_cap.lines", "value": 999}),
        "corrupt line",
        json.dumps({"key": "output_cap.lines", "value": 10}),
        json.dumps({"key": "output_cap.head", "value": 3}),
        json.dumps({"key": "output_cap.tail", "value": 2}),
    ]) + "\n")
    new = _reply(_run(tmp_path, _payload(_seq(30)), {"CDX_VALUES_FILE": str(ledger)}))
    lines = new["stdout"].splitlines()
    assert lines[:3] == ["1", "2", "3"] and lines[-2:] == ["29", "30"] and len(lines) == 6


def test_stderr_capped_separately_and_flags_preserved(tmp_path):
    new = _reply(_run(tmp_path, _payload("ok\n", stderr=_seq(500), interrupted=True)))
    assert new["stdout"] == "ok\n" and new["interrupted"] is True and "lines cut" in new["stderr"]


def test_pytest_keeps_tail_and_failed_summary(tmp_path):
    out = _seq(300) + "FAILED tests/x.py::t - assert 1 == 2\n" + _seq(300)
    new = _reply(_run(tmp_path, _payload(out, cmd="python -m pytest -q")))
    lines = new["stdout"].splitlines()
    assert lines[0].startswith("[cdx: ") and "FAILED tests/x.py::t - assert 1 == 2" in lines and lines[-1] == "300"


def test_malformed_stdin_is_silent(tmp_path):
    for stdin in ("not json", "", "[]", json.dumps({"tool_name": "Bash", "tool_response": "str"})):
        r = _run(tmp_path, None, stdin=stdin)
        assert r.returncode == 0 and r.stdout == ""


def test_output_filter_prints_nothing_for_long_command(tmp_path):
    r = subprocess.run([sys.executable, str(HOOKS / "output_filter.py")], text=True, capture_output=True,
                       input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "seq 1 500"}}))
    assert r.returncode == 0 and r.stdout == ""
