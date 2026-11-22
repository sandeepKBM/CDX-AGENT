"""Watch many Slurm jobs, tmux sessions, and log files from one process.

    python -m cdx_agent.watch --spec FILE.json [--out-dir DIR]
    python -m cdx_agent.watch --job ID[:NAME] --tmux NAME[:LOG[:DONE_RE[:FAIL_RE]]]
                                --log NAME:PATH:DONE_RE[:FAIL_RE]
                                [--interval 120] [--max-hours 12] [--keep-going]
                                [--offload-cmd cdx-agent]

The JSON spec is a list (or ``{"items": [...]}``) of objects.  An object may
contain ``job``, ``tmux``, or ``log`` in the same colon-separated forms as the
command line, plus optional ``summary`` and ``repo`` fields.  Each completed
summary is generated with ``cdx-agent --offload --repo REPO --timeout 1800``.
Slurm queries use ``ssh -o BatchMode=yes -o ConnectTimeout=10`` to
``$CDX_SLURM_HOST`` or ``rlab1``; tmux and log checks are local.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple


EXIT_OK = 0
EXIT_FAILED = 2
EXIT_TIMEOUT = 3


@dataclass
class WatchItem:
    """One target in the watch state machine."""

    item: str
    kind: str
    target: str
    log: Optional[str] = None
    done_re: Optional[str] = None
    fail_re: Optional[str] = None
    summary: Optional[str] = None
    repo: Optional[str] = None
    state: str = "pending"
    since: str = ""
    detail: str = ""
    summary_path: Optional[str] = None
    _summary_started: bool = field(default=False, repr=False)


@dataclass
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def _slurm(command: str, host: Optional[str] = None) -> List[str]:
    host = host or os.environ.get("CDX_SLURM_HOST", "rlab1")
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, command]


def run_command(command: Sequence[str]) -> CommandResult:
    """Run one local or SSH command; this is the seam used by tests."""
    # a Codex summary (--offload, its own --timeout 1800) runs for minutes: give it that budget, not the 60 s status-poll limit
    timeout = 1900 if "--offload" in command else 60
    try:
        result = subprocess.run(list(command), capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CommandResult(1, "", str(exc))
    return CommandResult(result.returncode, result.stdout or "", result.stderr or "")


def _result(value: Any) -> CommandResult:
    if isinstance(value, CommandResult):
        return value
    if isinstance(value, str):
        return CommandResult(0, value, "")
    if isinstance(value, tuple):
        if len(value) == 2:
            return CommandResult(int(value[0]), str(value[1]), "")
        if len(value) >= 3:
            return CommandResult(int(value[0]), str(value[1]), str(value[2]))
    return CommandResult(int(getattr(value, "returncode", 1)),
                        str(getattr(value, "stdout", "") or ""),
                        str(getattr(value, "stderr", "") or ""))


def _call(runner: Callable[[Sequence[str]], Any], command: Sequence[str]) -> CommandResult:
    try:
        return _result(runner(command))
    except TypeError:
        # A few tiny test doubles accept a list only through a positional tuple.
        return _result(runner(list(command)))


def _split_spec(value: str, fields: int) -> List[str]:
    parts = value.split(":", fields - 1)
    if any(not part for part in parts):
        raise ValueError(f"invalid watch spec: {value!r}")
    return parts


def parse_job(value: str, *, summary: Optional[str] = None, repo: Optional[str] = None) -> WatchItem:
    """Parse ``ID[:NAME]`` into a job item."""
    job_id, *rest = _split_spec(value, 2)
    name = rest[0] if rest else f"job-{job_id}"
    return WatchItem(name, "job", job_id, summary=summary, repo=repo)


def parse_tmux(value: str, *, summary: Optional[str] = None, repo: Optional[str] = None) -> WatchItem:
    """Parse ``NAME[:LOG[:DONE_RE[:FAIL_RE]]]`` into a tmux item."""
    parts = value.split(":", 3)
    if not parts[0]:
        raise ValueError(f"invalid tmux spec: {value!r}")
    while len(parts) < 4:
        parts.append("")
    return WatchItem(parts[0], "tmux", parts[0], log=parts[1] or None,
                     done_re=parts[2] or None, fail_re=parts[3] or None,
                     summary=summary, repo=repo)


def parse_log(value: str, *, summary: Optional[str] = None, repo: Optional[str] = None) -> WatchItem:
    """Parse ``NAME:PATH:DONE_RE[:FAIL_RE]`` into a log item."""
    name, path, done_re, *rest = _split_spec(value, 4)
    return WatchItem(name, "log", path, log=path, done_re=done_re,
                     fail_re=rest[0] if rest and rest[0] else None,
                     summary=summary, repo=repo)


def _mapping_item(raw: Mapping[str, Any], index: int) -> WatchItem:
    summary = raw.get("summary")
    repo = raw.get("repo")
    name = raw.get("item") or raw.get("name")
    if "job" in raw or raw.get("kind") == "job" or raw.get("type") == "job":
        value = raw.get("job", raw.get("id", raw.get("job_id")))
        if isinstance(value, Mapping):
            job_id = str(value.get("id", value.get("job_id", "")))
            value = f"{job_id}:{value.get('name')}" if value.get("name") else job_id
        item = parse_job(str(value), summary=summary, repo=repo)
    elif "tmux" in raw or raw.get("kind") == "tmux" or raw.get("type") == "tmux":
        value = raw.get("tmux")
        if value is None:
            value = ":".join(str(raw.get(key, "")) for key in ("name", "log", "done_re", "fail_re")).rstrip(":")
        if isinstance(value, Mapping):
            value = ":".join(str(value.get(key, "")) for key in ("name", "log", "done_re", "fail_re")).rstrip(":")
        item = parse_tmux(str(value), summary=summary, repo=repo)
    elif "log" in raw or raw.get("kind") == "log" or raw.get("type") == "log":
        value = raw.get("log")
        if value is None:
            value = ":".join(str(raw.get(key, "")) for key in ("name", "path", "done_re", "fail_re")).rstrip(":")
        if isinstance(value, Mapping):
            value = ":".join(str(value.get(key, "")) for key in ("name", "path", "done_re", "fail_re")).rstrip(":")
        item = parse_log(str(value), summary=summary, repo=repo)
    else:
        raise ValueError(f"spec item {index} has no job, tmux, or log")
    if name:
        item.item = str(name)
    return item


def load_spec(path: str) -> List[WatchItem]:
    """Load a JSON list of watch items, accepting a top-level ``items`` key."""
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    if isinstance(raw, Mapping):
        raw = raw.get("items", [])
    if not isinstance(raw, list):
        raise ValueError("watch spec must be a JSON list")
    return [_mapping_item(item, i) for i, item in enumerate(raw) if isinstance(item, Mapping)]


def parse_sacct_state(text: str) -> Optional[str]:
    """Return the first Slurm accounting state, without a step suffix."""
    for line in text.splitlines():
        state = line.strip().split()[0] if line.strip() else ""
        if state:
            return state.upper().split("+", 1)[0]
    return None


def parse_job_present(text: str, job_id: str) -> bool:
    """Whether a compact ``squeue`` response still contains ``job_id``."""
    wanted = str(job_id)
    return any(line.strip().split()[0] == wanted for line in text.splitlines() if line.strip())


def _regex_matches(path: Optional[str], pattern: Optional[str]) -> bool:
    if not path or not pattern:
        return False
    try:
        return re.search(pattern, Path(path).read_text(errors="replace")) is not None
    except (OSError, re.error):
        return False


def _log_matches_text(path: Optional[str], pattern: Optional[str]) -> bool:
    return _regex_matches(path, pattern)


def check_item(item: WatchItem, runner: Optional[Callable[[Sequence[str]], Any]] = None,
               slurm_host: Optional[str] = None) -> Tuple[str, str]:
    """Check an item once and return ``(state, detail)`` without changing it."""
    runner = runner or run_command
    if item.kind == "job":
        queue = _call(runner, _slurm(f"squeue -j {shlex.quote(item.target)} -h -o %i", slurm_host))
        if queue.returncode != 0:
            return "running", "squeue unavailable"
        if parse_job_present(queue.stdout, item.target):
            return "running", "in squeue"
        accounting = _call(runner, _slurm(f"sacct -j {shlex.quote(item.target)} -n -X -o State", slurm_host))
        state = parse_sacct_state(accounting.stdout) if accounting.returncode == 0 else None
        if state is None:
            return "running", "left squeue; waiting for sacct"
        if state == "COMPLETED":
            return "done", state
        return "failed", state

    if item.kind == "log":
        if _log_matches_text(item.log, item.fail_re):
            return "failed", f"fail regex matched {item.log}"
        if _log_matches_text(item.log, item.done_re):
            return "done", f"done regex matched {item.log}"
        return "running", "waiting for log regex"

    # A fail regex wins even if the tmux session has already gone away.
    if _log_matches_text(item.log, item.fail_re):
        return "failed", f"fail regex matched {item.log}"
    if _log_matches_text(item.log, item.done_re):
        return "done", f"done regex matched {item.log}"
    session = _call(runner, ["tmux", "has-session", "-t", item.target])
    if session.returncode != 0:
        return "done", "tmux session gone"
    return "running", "tmux session active"


def _stamp(now: Optional[float] = None) -> str:
    return datetime.fromtimestamp(now if now is not None else time.time(), timezone.utc).isoformat(timespec="seconds")


def _safe_filename(name: str) -> str:
    result = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._")
    return result or "item"


def _write_status(out_dir: Path, items: Sequence[WatchItem], events: Sequence[Mapping[str, str]]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    if events:
        with (out_dir / "status.jsonl").open("a", encoding="utf-8") as fh:
            for event in events:
                fh.write(json.dumps(dict(event), sort_keys=True) + "\n")
    lines = ["# Watch status", "", "| item | state | since | summary |", "|---|---|---|---|"]
    for item in items:
        summary = item.summary_path or "-"
        lines.append(f"| {item.item} | {item.state} | {item.since} | {summary} |")
    (out_dir / "status.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _summary_command(item: WatchItem, offload_cmd: str, cwd: str) -> List[str]:
    command = shlex.split(offload_cmd) if isinstance(offload_cmd, str) else list(offload_cmd)
    repo = item.repo or cwd
    return command + ["--offload", "--repo", repo, "--timeout", "1800", item.summary or ""]


def _run_summary(item: WatchItem, out_dir: Path, offload_cmd: str,
                 cwd: str, runner: Callable[[Sequence[str]], Any]) -> Optional[str]:
    result = _call(runner, _summary_command(item, offload_cmd, cwd))
    if result.returncode != 0:
        return None
    path = out_dir / f"{_safe_filename(item.item)}.md"
    path.write_text(result.stdout, encoding="utf-8")
    return str(path)


def _summary_error(item: WatchItem, out_dir: Path) -> str:
    """The run finished; only its summary failed: note it next to the status (the run is NOT marked failed)."""
    path = out_dir / f"{_safe_filename(item.item)}.md"
    path.write_text("summary command failed (run finished normally); rerun: cdx-agent --offload with the spec's summary prompt\n",
                    encoding="utf-8")
    return str(path)


def watch_items(items: Sequence[WatchItem], out_dir: str, *, interval: float = 120.0,
                max_hours: float = 12.0, keep_going: bool = False,
                offload_cmd: str = "cdx-agent", slurm_host: Optional[str] = None,
                runner: Optional[Callable[[Sequence[str]], Any]] = None,
                sleep_fn: Callable[[float], None] = time.sleep,
                now_fn: Callable[[], float] = time.time,
                cwd: Optional[str] = None) -> int:
    """Run the polling state machine, with all external operations injectable."""
    out = Path(out_dir)
    runner = runner or run_command
    workdir = cwd or os.getcwd()
    started = now_fn()
    events: List[Mapping[str, str]] = []
    for item in items:
        item.state = "pending"
        item.since = _stamp(started)
        item.detail = "not checked"
        events.append({"ts": item.since, "item": item.item, "state": item.state, "detail": item.detail})
    _write_status(out, items, events)

    while True:
        events = []
        failed = False
        unresolved = False
        for item in items:
            if item.state in {"done", "failed"}:
                failed = failed or item.state == "failed"
                continue
            unresolved = True
            state, detail = check_item(item, runner, slurm_host)
            if state == "running":
                if item.state == "pending":
                    item.state = "running"
                    item.since = _stamp(now_fn())
                    events.append({"ts": item.since, "item": item.item, "state": item.state, "detail": detail})
                item.detail = detail
                continue
            if state == "done" and item.summary:
                item.detail = "finished; running summary"
                summary_path = _run_summary(item, out, offload_cmd, workdir, runner)
                if summary_path is None:
                    item.summary_path = _summary_error(item, out)
                    detail = "finished; summary failed"
                else:
                    item.summary_path = summary_path
            item.state = state
            item.detail = detail
            item.since = _stamp(now_fn())
            events.append({"ts": item.since, "item": item.item, "state": state, "detail": detail})
            if state == "failed":
                failed = True
                if not keep_going:
                    _write_status(out, items, events)
                    return EXIT_FAILED
        _write_status(out, items, events)
        if all(item.state == "done" for item in items):
            return EXIT_OK
        if failed and not keep_going:
            return EXIT_FAILED
        if max_hours >= 0 and now_fn() - started >= max_hours * 3600:
            for item in items:
                if item.state not in {"done", "failed"}:
                    item.state = "failed"
                    item.since = _stamp(now_fn())
                    item.detail = "watch timeout"
                    _write_status(out, items, [{"ts": item.since, "item": item.item,
                                                "state": item.state, "detail": item.detail}])
            return EXIT_TIMEOUT
        if not unresolved:
            return EXIT_FAILED if failed else EXIT_OK
        sleep_fn(max(interval, 0.0))


def _items_from_args(args: argparse.Namespace) -> List[WatchItem]:
    items: List[WatchItem] = []
    if args.spec:
        items.extend(load_spec(args.spec))
    items.extend(parse_job(value) for value in args.job)
    items.extend(parse_tmux(value) for value in args.tmux)
    items.extend(parse_log(value) for value in args.log)
    if not items:
        raise ValueError("at least one watch item is required")
    names: Dict[str, int] = {}
    for item in items:
        names[item.item] = names.get(item.item, 0) + 1
    for index, item in enumerate(items, 1):
        if names[item.item] > 1:
            item.item = f"{item.item}-{index}"
    return items


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m cdx_agent.watch", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--spec", default=None)
    p.add_argument("--job", action="append", default=[], metavar="ID[:NAME]")
    p.add_argument("--tmux", action="append", default=[], metavar="NAME[:LOG[:DONE_RE[:FAIL_RE]]]")
    p.add_argument("--log", action="append", default=[], metavar="NAME:PATH:DONE_RE[:FAIL_RE]")
    p.add_argument("--out-dir", default="watch-status")
    p.add_argument("--interval", type=float, default=120.0)
    p.add_argument("--max-hours", type=float, default=12.0)
    p.add_argument("--keep-going", action="store_true")
    p.add_argument("--offload-cmd", default="cdx-agent")
    p.add_argument("--slurm-host", default=None)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        items = _items_from_args(args)
        code = watch_items(items, args.out_dir, interval=args.interval, max_hours=args.max_hours,
                           keep_going=args.keep_going, offload_cmd=args.offload_cmd,
                           slurm_host=args.slurm_host)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"watch: {exc}", file=sys.stderr)
        return EXIT_FAILED
    counts: Dict[str, int] = {"done": 0, "failed": 0, "running": 0, "pending": 0}
    for item in items:
        counts[item.state] = counts.get(item.state, 0) + 1
    print(f"done={counts.get('done', 0)} failed={counts.get('failed', 0)} pending={counts.get('pending', 0) + counts.get('running', 0)} status={Path(args.out_dir) / 'status.md'}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
