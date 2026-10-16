"""cdx_agent.notify: the one-shot JSONL notifier (anomaly / settle / stale heartbeat), read-only on EVENTS."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from cdx_agent import notify

SRC = str(Path(__file__).resolve().parents[1] / "src")


def _append(path: Path, kind: str, text: str, t: str = "2026-10-16T12:00:00") -> None:
    with path.open("a") as fh:
        fh.write(json.dumps({"t": t, "kind": kind, "text": text}) + "\n")


def _fake_logger(events: Path, script, beat: float = 0.05, stop: threading.Event | None = None) -> threading.Thread:
    """Plays `script` [(delay_s, kind, text)] into events while touching the heartbeat."""
    alive = Path(str(events) + ".alive")

    def run() -> None:
        steps = list(script)
        t0 = time.time()
        while not (stop and stop.is_set()) and time.time() - t0 < 10:
            alive.touch()
            while steps and time.time() - t0 >= steps[0][0]:
                _, kind, text = steps.pop(0)
                _append(events, kind, text)
            time.sleep(beat)

    th = threading.Thread(target=run, daemon=True)
    th.start()
    return th


@pytest.fixture
def events(tmp_path):
    ev = tmp_path / "events.jsonl"
    _append(ev, "ERROR", "old anomaly before the notifier started (must be ignored)")
    Path(str(ev) + ".alive").touch()
    return ev


def test_load_routine_skips_comments_and_rejects_bad_regex(tmp_path):
    f = tmp_path / "routine.txt"
    f.write_text("# comment\n\nWATCH \nNOTE HTTP 426\n")
    pats = notify.load_routine(str(f))
    assert [p.pattern for p in pats] == ["WATCH ", "NOTE HTTP 426"]
    assert notify.is_routine("WATCH serving since x", pats)
    assert not notify.is_routine("xx WATCH", pats)  # re.match: anchored at the start
    f.write_text("ok\n(unclosed\n")
    with pytest.raises(ValueError, match=":2:"):
        notify.load_routine(str(f))


def test_tail_waits_for_complete_lines_and_handles_truncation(tmp_path):
    f = tmp_path / "e.jsonl"
    f.write_text('{"kind": "A"}\n')
    tail = notify.Tail(str(f))
    assert tail.read_lines() == []  # starts at the end
    with f.open("a") as fh:
        fh.write('{"kind": "B"}\n{"kind": "C"')  # C is half-written
    assert tail.read_lines() == ['{"kind": "B"}']
    with f.open("a") as fh:
        fh.write("}\n")
    assert tail.read_lines() == ['{"kind": "C"}']
    f.write_text('{"kind": "D"}\n')  # logger truncated + restarted
    assert tail.read_lines() == ['{"kind": "D"}']
    assert tail.truncations == 1


def test_anomaly_mode(events):
    routine = notify.load_routine(None) + [__import__("re").compile(r"WATCH ")]
    _fake_logger(events, [(0.1, "WATCH", "ok"), (0.2, "END", "trial 1 done"), (0.3, "EXC", "boom in policy")])
    rc, why = notify.watch(str(events), routine, settle=30, stale=5, poll=0.05)
    assert rc == notify.EXIT_ANOMALY
    assert why.startswith("ANOMALY 2026-10-16T12:00:00 EXC boom in policy")


def test_done_mode_waits_for_settle(events):
    import re

    stop = threading.Event()
    _fake_logger(events, [(0.1, "WATCH", "ok"), (0.15, "END", "a"), (0.25, "EMPTY", "b")], stop=stop)
    t0 = time.time()
    rc, why = notify.watch(str(events), [re.compile("WATCH")], settle=0.6, stale=5, poll=0.05)
    stop.set()
    assert rc == notify.EXIT_DONE
    assert why.startswith("DONE: 2 done-kind event(s)")
    assert time.time() - t0 >= 0.7  # 0.15 s to the first END + 0.6 s settle


def test_stale_heartbeat_mode(events):
    alive = str(events) + ".alive"
    old = time.time() - 100
    os.utime(alive, (old, old))
    rc, why = notify.watch(str(events), [], stale=5, poll=0.05)
    assert rc == notify.EXIT_STALE and "stale" in why
    os.remove(alive)
    rc, why = notify.watch(str(events), [], stale=5, poll=0.05)
    assert rc == notify.EXIT_STALE and "missing" in why


def test_max_wait_and_stale_disabled(events):
    os.remove(str(events) + ".alive")
    rc, why = notify.watch(str(events), [], stale=0, poll=0.05, max_wait=0.2)
    assert rc == notify.EXIT_TIMEOUT


def test_cli_runs_then_and_never_writes_events(events, tmp_path):
    routine = tmp_path / "routine.txt"
    routine.write_text("# routine\nWATCH\n")
    before = events.read_bytes()
    _fake_logger(events, [(0.2, "WATCH", "fine"), (0.3, "PROTO", "bad frame")])
    r = subprocess.run(
        [sys.executable, "-m", "cdx_agent.notify", str(events), "--routine", str(routine), "--poll", "0.05",
         "--stale", "5", "--then", "echo ledger-update"],
        capture_output=True, text=True, timeout=30, env=dict(os.environ, PYTHONPATH=SRC),
    )
    assert r.returncode == notify.EXIT_ANOMALY, r.stderr
    lines = r.stdout.splitlines()
    assert lines[0].startswith("ANOMALY") and "PROTO bad frame" in lines[0]
    assert "ledger-update" in lines and "then_rc=0" in lines
    assert events.read_bytes().startswith(before)  # only the fake logger appended


def test_cli_missing_events_file(tmp_path):
    assert notify.main([str(tmp_path / "nope.jsonl")]) == 1
