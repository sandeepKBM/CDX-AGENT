import json
import os
import time

from cdx_agent import agent_usage


def record(kind, message):
    return json.dumps({"type": kind, "message": message})


def test_parse_transcript_usage_waiting_and_prompt(tmp_path):
    path = tmp_path / "aabc123.output"
    rows = [
        record("user", {"role": "user", "content": "AGENT: inspect the queue\nextra prompt"}),
        record("assistant", {"role": "assistant", "model": "opus", "usage": {
            "input_tokens": 10, "cache_read_input_tokens": 20,
            "cache_creation_input_tokens": 3, "output_tokens": 4,
        }, "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                              "input": {"command": "while true; do sleep 30; done"}}]}),
    ]
    path.write_text("\n".join(rows) + "\n")
    old = time.time() - 20 * 60
    os.utime(path, (old, old))
    report = agent_usage.parse_transcript(path, now=time.time(), idle_min=10)
    assert report.prompt == "AGENT: inspect the queue"
    assert report.models == ["opus"]
    assert (report.input_tokens, report.cache_read, report.cache_creation, report.output_tokens) == (10, 20, 3, 4)
    assert report.last_tool.startswith("while true")
    assert "WAITING" in report.flags


def test_discovery_skips_shell_logs_and_active_only(tmp_path):
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    fresh = tasks / "a111.output"
    fresh.write_text(record("user", {"role": "user", "content": "hello"}) + "\n")
    old = tasks / "a222.output"
    old.write_text(record("user", {"role": "user", "content": "old"}) + "\n")
    old_time = time.time() - 120 * 60
    os.utime(old, (old_time, old_time))
    (tasks / "b333.output").write_text("should be skipped\n")
    reports = agent_usage.collect_reports([str(tasks)], now=time.time(), active_only=False)
    assert [report.id for report in reports] == ["a111", "a222"]
    assert "IDLE" in reports[1].flags
    active = agent_usage.collect_reports([str(tasks)], now=time.time(), active_only=True)
    assert [report.id for report in active] == ["a111"]


def test_json_output_and_big_flag(tmp_path, capsys):
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    path = tasks / "adeadbeef.output"
    path.write_text(record("user", {"role": "user", "content": "prompt"}) + "\n" + "x" * (1024 * 1024 + 5))
    assert agent_usage.main(["--tasks-dir", str(tasks), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["id"] == "adeadbeef"
    assert "BIG" in data[0]["flags"]
    assert "x" not in capsys.readouterr().out


def _tu(tid, name, **inp):
    return record("assistant", {"role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]})


def _tr(tid):
    return record("user", {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": "ok"}]})


def test_waiting_clears_after_later_tool_use(tmp_path):
    """F17: only the LAST tool use decides WAITING; a sleep followed by a Read is not waiting."""
    path = tmp_path / "a1.output"
    path.write_text("\n".join([_tu("t1", "Bash", command="sleep 30"), _tr("t1"), _tu("t2", "Read", file_path="/x"), _tr("t2")]) + "\n")
    assert "WAITING" not in agent_usage.parse_transcript(path).flags
    path.write_text("\n".join([_tu("t1", "Bash", command="sleep 30"), _tr("t1"), _tu("t2", "Bash", command="ls")]) + "\n")
    assert "WAITING" not in agent_usage.parse_transcript(path).flags
    text = record("assistant", {"role": "assistant", "content": [{"type": "text", "text": "done"}]})
    path.write_text("\n".join([_tu("t1", "Bash", command="sleep 30"), _tr("t1"), text]) + "\n")
    assert "WAITING" not in agent_usage.parse_transcript(path).flags
    path.write_text(_tu("t1", "Bash", command="sleep 30") + "\n")
    assert "WAITING" in agent_usage.parse_transcript(path).flags


def test_active_only_excludes_finished_transcript(tmp_path):
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    working = tasks / "a111.output"
    working.write_text(record("user", {"role": "user", "content": "go"}) + "\n"
                       + record("assistant", {"role": "assistant", "content": [
                           {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/x"}}]}) + "\n")
    done = tasks / "a222.output"
    done.write_text(record("user", {"role": "user", "content": "go"}) + "\n"
                    + record("assistant", {"role": "assistant", "content": [{"type": "text", "text": "final report"}]}) + "\n")
    reports = agent_usage.collect_reports([str(tasks)], now=time.time(), active_only=False)
    assert {r.id: ("DONE" in r.flags) for r in reports} == {"a111": False, "a222": True}
    active = agent_usage.collect_reports([str(tasks)], now=time.time(), active_only=True)
    assert [r.id for r in active] == ["a111"]
