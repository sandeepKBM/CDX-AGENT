import json
import os

from cdx_agent import notify
from cdx_agent.runlog import RunLog


def test_events_and_alive(tmp_path):
    d = str(tmp_path / "r")
    log = RunLog(d)
    log.start(name="x")
    assert os.path.exists(os.path.join(d, ".alive")) and os.path.exists(os.path.join(d, "events.jsonl.alive"))
    log.unit("u1", state="done")
    log.end("done", exit_code=0)
    assert not os.path.exists(os.path.join(d, ".alive"))
    with open(os.path.join(d, "events.jsonl")) as fh:
        rows = [json.loads(x) for x in fh]
    assert [r["event"] for r in rows] == ["START", "UNIT", "END"]
    assert all(r["kind"] == r["event"] and "t" in r and "time" in r for r in rows)
    assert rows[1]["id"] == "u1"


def test_notify_fields(tmp_path):
    d = str(tmp_path / "r")
    log = RunLog(d)
    log.start(name="x")
    log.end("done")
    with open(os.path.join(d, "events.jsonl")) as fh:
        ev = [json.loads(x) for x in fh]
    assert ev[-1]["kind"] in ("END", "EMPTY")
    assert notify.event_line(ev[0]).startswith("START")
