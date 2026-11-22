import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from cdx_agent import teardown

FAKE_TMUX = r"""#!/bin/sh
# state file: one "window<TAB>start command" per line; no file = no such session
case "$1" in
  has-session) [ -f "$FAKE_TMUX_STATE" ] ;;
  list-windows) cat "$FAKE_TMUX_STATE" ;;
  kill-window) printf '%s\n' "$*" >> "$FAKE_TMUX_LOG" ;;
  *) exit 1 ;;
esac
"""
FAKE_SS = "#!/bin/sh\nexit 0\n"


@pytest.fixture()
def fake(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("tmux", FAKE_TMUX), ("ss", FAKE_SS), ("sleep", "#!/bin/sh\nexit 0\n")):
        p = bindir / name
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IXUSR)
    state = tmp_path / "tmux_state"
    log = tmp_path / "tmux_kill.log"
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", FAKE_TMUX_STATE=str(state), FAKE_TMUX_LOG=str(log))
    return env, state, log


def _spec(tmp_path, **over):
    spec = {"name": "svc", "tmux_session": "sess", "groups": [["api server", "worker"], ["db"]],
            "expect": {"api server": "serve.py", "db": "postgres"}, "busy_ports": [], "keep_listening": [],
            "log": str(tmp_path / "logs" / "events.jsonl"), "after_cmd": ""}
    spec.update(over)
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(spec))
    return p


def _generate(tmp_path, **over):
    spec = _spec(tmp_path, **over)
    assert teardown.main([str(spec)]) == 0
    return tmp_path / "stop_svc.sh"


def _run(script, env, confirm=False):
    e = dict(env)
    if confirm:
        e["CONFIRM"] = "1"
    return subprocess.run(["bash", str(script)], env=e, capture_output=True, text=True, timeout=60)


def test_generated_script_is_valid_bash_and_executable(tmp_path):
    script = _generate(tmp_path)
    assert os.access(script, os.X_OK)
    assert subprocess.run(["bash", "-n", str(script)]).returncode == 0


def test_dry_run_with_spaced_window_names(tmp_path, fake):
    env, state, log = fake
    state.write_text("api server\tpython serve.py\nworker\tpython w.py\ndb\tpostgres -D x\n")
    res = _run(_generate(tmp_path), env)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "ok   api server" in res.stdout
    assert "worker (no expect)" in res.stdout
    assert "dry run OK" in res.stdout
    assert not log.exists()


def test_confirm_kills_each_window_whole_and_logs_valid_json(tmp_path, fake):
    env, state, log = fake
    state.write_text("api server\tpython serve.py\nworker\tpython w.py\ndb\tpostgres -D x\n")
    res = _run(_generate(tmp_path), env, confirm=True)
    assert res.returncode == 0, res.stdout + res.stderr
    kills = log.read_text().splitlines()
    assert kills == ["kill-window -t sess:=api server", "kill-window -t sess:=worker", "kill-window -t sess:=db"]
    events = [json.loads(line) for line in (tmp_path / "logs" / "events.jsonl").read_text().splitlines()]
    assert [e["kind"] for e in events] == ["START", "KILL", "KILL", "KILL", "DONE"]
    assert events[1]["text"] == "api server"


def test_event_log_escapes_quotes_in_window_names(tmp_path, fake):
    env, state, log = fake
    name = 'we"ird\\name'
    state.write_text(f"{name}\tx\n")
    res = _run(_generate(tmp_path, groups=[[name]], expect={}), env, confirm=True)
    assert res.returncode == 0, res.stdout + res.stderr
    events = [json.loads(line) for line in (tmp_path / "logs" / "events.jsonl").read_text().splitlines()]
    assert events[1] == {**events[1], "kind": "KILL", "text": name}


def test_missing_session_is_reported_missing_not_gone(tmp_path, fake):
    env, state, log = fake   # state file absent -> has-session fails
    res = _run(_generate(tmp_path), env)
    assert res.returncode == 0
    assert "missing tmux session sess" in res.stdout
    assert "gone" not in res.stdout


def test_missing_window_in_existing_session_is_gone(tmp_path, fake):
    env, state, log = fake
    state.write_text("db\tpostgres\n")
    res = _run(_generate(tmp_path), env)
    assert res.returncode == 0
    assert "gone api server (already stopped)" in res.stdout
    assert "gone worker (already stopped)" in res.stdout


def test_identity_mismatch_aborts_without_killing(tmp_path, fake):
    env, state, log = fake
    state.write_text("api server\tpython other.py\nworker\tx\ndb\tpostgres\n")
    res = _run(_generate(tmp_path), env, confirm=True)
    assert res.returncode == 1
    assert "FAIL api server start command does not contain 'serve.py'" in res.stdout
    assert not log.exists()


def test_cli_errors_exit_2(tmp_path, capsys):
    assert teardown.main([]) == 2
    assert teardown.main([str(tmp_path / "nope.json")]) == 2
    spec = _spec(tmp_path)
    assert teardown.main([str(spec), "--out"]) == 2
    assert teardown.main([str(spec), "--bogus"]) == 2
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert teardown.main([str(bad)]) == 2
    missing = tmp_path / "missing.json"
    missing.write_text(json.dumps({"name": "x"}))
    assert teardown.main([str(missing)]) == 2
    assert "missing 'tmux_session'" in capsys.readouterr().err


def test_out_option_and_module_entry_point(tmp_path):
    spec = _spec(tmp_path)
    out = tmp_path / "custom.sh"
    res = subprocess.run([sys.executable, "-m", "cdx_agent.teardown", str(spec), "--out", str(out)], capture_output=True,
                         text=True, env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src")))
    assert res.returncode == 0 and out.is_file()
    assert res.stdout.strip() == f"wrote {out}"
