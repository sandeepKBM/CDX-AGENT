import json
from pathlib import Path

from cdx_agent import watch


class Runner:
    def __init__(self, *, job_active=True, sacct="COMPLETED", tmux_code=0, summary="summary\n"):
        self.job_active = job_active
        self.sacct = sacct
        self.tmux_code = tmux_code
        self.summary = summary
        self.calls = []

    def __call__(self, command):
        command = list(command)
        self.calls.append(command)
        if command and command[0] == "tmux":
            return watch.CommandResult(self.tmux_code, "", "")
        if command and command[0] == "fake-offload":
            return watch.CommandResult(0, self.summary, "")
        remote = command[-1]
        if remote.startswith("squeue"):
            return watch.CommandResult(0, "123\n" if self.job_active else "", "")
        if remote.startswith("sacct"):
            return watch.CommandResult(0, self.sacct + "\n", "")
        raise AssertionError(command)


def test_job_leaves_queue_and_summary_is_written(tmp_path):
    runner = Runner(job_active=False)
    item = watch.parse_job("123:train", summary="summarize it", repo="/repo")
    code = watch.watch_items([item], str(tmp_path), interval=0, offload_cmd="fake-offload",
                             runner=runner, sleep_fn=lambda _: None)
    assert code == 0
    assert (tmp_path / "train.md").read_text() == "summary\n"
    assert "state | since | summary" in (tmp_path / "status.md").read_text()
    events = [json.loads(line) for line in (tmp_path / "status.jsonl").read_text().splitlines()]
    assert [event["state"] for event in events] == ["pending", "done"]
    summary_call = next(call for call in runner.calls if call[0] == "fake-offload")
    assert summary_call[1:] == ["--offload", "--repo", "/repo", "--timeout", "1800", "summarize it"]


def test_job_failed_accounting_exits_immediately(tmp_path):
    runner = Runner(job_active=False, sacct="FAILED")
    item = watch.parse_job("123")
    assert watch.watch_items([item], str(tmp_path), runner=runner, interval=0) == 2
    assert item.state == "failed"
    assert "FAILED" in (tmp_path / "status.jsonl").read_text()


def test_tmux_done_and_fail_regex(tmp_path):
    log = tmp_path / "tmux.log"
    log.write_text("step complete\n")
    done = watch.parse_tmux(f"sess:{log}:complete")
    assert watch.check_item(done, Runner(tmux_code=0))[0] == "done"

    log.write_text("ERROR: failed\n")
    failed = watch.parse_tmux(f"sess:{log}:complete:ERROR")
    assert watch.check_item(failed, Runner(tmux_code=0)) == ("failed", f"fail regex matched {log}")


def test_tmux_session_gone_is_done_and_timeout_is_three(tmp_path):
    gone = watch.parse_tmux("gone")
    assert watch.check_item(gone, Runner(tmux_code=1)) == ("done", "tmux session gone")
    active = watch.parse_tmux("active")
    assert watch.watch_items([active], str(tmp_path), max_hours=0, interval=0,
                             runner=Runner(tmux_code=0), sleep_fn=lambda _: None) == 3


def test_load_spec_and_log_item(tmp_path):
    log = tmp_path / "run.log"
    log.write_text("DONE\n")
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps([{"log": f"run:{log}:DONE", "summary": "report", "repo": str(tmp_path)}]))
    items = watch.load_spec(str(spec))
    assert items[0].item == "run" and items[0].summary == "report"
    assert watch.check_item(items[0])[0] == "done"


def test_summary_failure_keeps_run_done_and_exit_zero(tmp_path):
    class FailingSummary(Runner):
        def __call__(self, command):
            if list(command)[0] == "fake-offload":
                self.calls.append(list(command))
                return watch.CommandResult(1, "", "timed out")
            return super().__call__(command)

    runner = FailingSummary(job_active=False)
    item = watch.parse_job("123:train", summary="summarize it", repo="/repo")
    code = watch.watch_items([item], str(tmp_path), interval=0, offload_cmd="fake-offload",
                             runner=runner, sleep_fn=lambda _: None)
    assert code == 0
    events = [json.loads(line) for line in (tmp_path / "status.jsonl").read_text().splitlines()]
    assert events[-1]["state"] == "done" and events[-1]["detail"] == "finished; summary failed"
    assert "summary command failed" in (tmp_path / "train.md").read_text()


def test_offload_commands_get_the_long_timeout(monkeypatch):
    seen = []

    def fake_run(command, **kw):
        seen.append(kw["timeout"])
        class R:
            returncode, stdout, stderr = 0, "", ""
        return R()

    monkeypatch.setattr(watch.subprocess, "run", fake_run)
    watch.run_command(["cdx-agent", "--offload", "--repo", "/r", "--timeout", "1800", "p"])
    watch.run_command(["tmux", "has-session", "-t", "x"])
    assert seen == [1900, 60]
