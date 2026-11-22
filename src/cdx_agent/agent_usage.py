"""Summarize Claude Code background-agent transcripts without printing them.

    python -m cdx_agent.agent_usage [--tasks-dir DIR ...] [--idle-min 10]
                                    [--active-only] [--json]

Claude stores agent transcripts as ``a<hex>.output`` JSONL files below
``~/.tmp/claude-*/*/*/tasks``.  ``b*.output`` shell-task logs are ignored.
The report includes model and token totals, file activity, a compact prompt
line, and the last tool action; transcript records themselves are never
printed.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set


AGENT_FILE = re.compile(r"^a[0-9a-f]+\.output$", re.IGNORECASE)
WAIT_COMMAND = re.compile(
    r"(?:\bsleep\s+\d+\b|tail\s+(?:-[^\s]*\s+)*--pid\b|\bwait\s+\$?\(?\d+\)?\b|"
    r"(?:\b(?:until|while)\b)[^\n;]*\bsleep\b)", re.IGNORECASE
)


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(text).replace("\x00", " ")).strip()
    return text if len(text) <= limit else text[: max(0, limit - 3)] + "..."


def _text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: List[str] = []
    for block in content:
        if not isinstance(block, Mapping):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


def _usage_value(usage: Mapping[str, Any], *names: str) -> int:
    for name in names:
        value = usage.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value)
    return 0


def _tool_text(block: Mapping[str, Any]) -> str:
    data = block.get("input", block.get("arguments", {}))
    if isinstance(data, Mapping):
        for key in ("command", "cmd", "description", "query"):
            if data.get(key) is not None:
                return str(data[key])
        try:
            return json.dumps(data, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            return str(data)
    return str(data) if data is not None else str(block.get("name", "tool_use"))


def _tool_id(block: Mapping[str, Any]) -> str:
    return str(block.get("id", block.get("tool_use_id", "")))


@dataclass
class AgentReport:
    id: str
    prompt: str
    models: List[str]
    input_tokens: int
    cache_read: int
    cache_creation: int
    output_tokens: int
    size_mb: float
    mtime: float
    minutes_since: float
    last_tool: str
    flags: List[str] = field(default_factory=list)
    path: str = field(default="", repr=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "prompt": self.prompt,
            "models": self.models,
            "input": self.input_tokens,
            "cache_read": self.cache_read,
            "cache_creation": self.cache_creation,
            "output": self.output_tokens,
            "transcript_mb": round(self.size_mb, 3),
            "last_activity": datetime.fromtimestamp(self.mtime, timezone.utc).isoformat(timespec="seconds"),
            "minutes_since": round(self.minutes_since, 1),
            "last_tool": self.last_tool,
            "flags": self.flags,
        }


def discover_tasks_dirs(tasks_dirs: Optional[Sequence[str]] = None) -> List[Path]:
    """Return explicit task directories or the default Claude glob."""
    if tasks_dirs:
        return [Path(path).expanduser() for path in tasks_dirs]
    root = Path("~/.tmp").expanduser()
    return sorted(root.glob("claude-*/*/*/tasks"), key=lambda path: str(path))


def discover_agent_files(tasks_dirs: Optional[Sequence[str]] = None) -> List[Path]:
    """Find transcript files, following symlinks for file metadata."""
    found: Dict[str, Path] = {}
    for directory in discover_tasks_dirs(tasks_dirs):
        try:
            children = directory.iterdir()
        except OSError:
            continue
        for path in children:
            if AGENT_FILE.match(path.name):
                try:
                    if path.is_file():
                        found[str(path.resolve())] = path
                except OSError:
                    continue
    return list(found.values())


def _prompt_line(prompts: Sequence[str]) -> str:
    for prompt in prompts:
        for line in prompt.splitlines():
            if re.match(r"^\s*AGENT\s*:", line, re.IGNORECASE):
                return _clip(line, 80)
    for prompt in prompts:
        for line in prompt.splitlines():
            if line.strip():
                return _clip(line, 60)
    return "-"


def parse_transcript(path: Path, *, now: Optional[float] = None, idle_min: float = 10.0) -> AgentReport:
    """Parse one JSONL transcript into report data, omitting malformed rows."""
    prompts: List[str] = []
    models: List[str] = []
    model_seen: Set[str] = set()
    totals = {"input": 0, "cache_read": 0, "cache_creation": 0, "output": 0}
    last_tool = "-"
    last_tool_name = ""
    last_tool_command = ""
    last_tool_key = ""
    pending_tools: Set[str] = set()
    last_record_was_tool_use = False
    anonymous_tool_pending = False
    anonymous_tool_number = 0
    try:
        stat = path.stat()
        size_mb = stat.st_size / (1024 * 1024)
        mtime = stat.st_mtime
    except OSError:
        stat = None
        size_mb = 0.0
        mtime = 0.0
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    record = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(record, Mapping):
                    continue
                message = record.get("message")
                if not isinstance(message, Mapping):
                    continue
                role = str(message.get("role", record.get("type", "")))
                content = message.get("content")
                last_record_was_tool_use = False
                if role == "user":
                    text = _text_content(content)
                    if text:
                        prompts.append(text)
                if role == "assistant" or record.get("type") == "assistant":
                    model = message.get("model", record.get("model"))
                    if model and str(model) not in model_seen:
                        model_seen.add(str(model))
                        models.append(str(model))
                    usage = message.get("usage")
                    if isinstance(usage, Mapping):
                        totals["input"] += _usage_value(usage, "input_tokens", "input")
                        totals["cache_read"] += _usage_value(usage, "cache_read_input_tokens", "cache_read")
                        totals["cache_creation"] += _usage_value(usage, "cache_creation_input_tokens", "cache_creation")
                        totals["output"] += _usage_value(usage, "output_tokens", "output")
                blocks = content if isinstance(content, list) else []
                tool_blocks = [block for block in blocks if isinstance(block, Mapping) and block.get("type") == "tool_use"]
                result_blocks = [block for block in blocks if isinstance(block, Mapping) and block.get("type") == "tool_result"]
                if tool_blocks:
                    last_record_was_tool_use = True
                    for block in tool_blocks:
                        tool_id = _tool_id(block)
                        if tool_id:
                            pending_tools.add(tool_id)
                            last_tool_key = tool_id
                        else:
                            anonymous_tool_number += 1
                            last_tool_key = f"__anonymous_{anonymous_tool_number}"
                            pending_tools.add(last_tool_key)
                            anonymous_tool_pending = True
                        last_tool_name = str(block.get("name", "tool"))
                        last_tool_command = _tool_text(block)
                        last_tool = _clip(last_tool_command, 80)
                elif result_blocks:
                    last_record_was_tool_use = False
                    for block in result_blocks:
                        tool_id = str(block.get("tool_use_id", block.get("id", "")))
                        if tool_id:
                            pending_tools.discard(tool_id)
                        elif anonymous_tool_pending:
                            anonymous_tool_pending = False
                            anonymous = [key for key in pending_tools if key.startswith("__anonymous_")]
                            if anonymous:
                                pending_tools.remove(anonymous[-1])
                elif role == "assistant":
                    last_record_was_tool_use = False
    except OSError:
        pass

    current = time.time() if now is None else now
    minutes = max(0.0, (current - mtime) / 60.0) if mtime else 0.0
    # F17: only the LAST tool use decides, and only while it has not returned (a finished sleep is not a wait).
    is_bash_wait = (last_tool_name.lower() == "bash" and bool(WAIT_COMMAND.search(last_tool_command))
                    and last_tool_key in pending_tools)
    no_result_wait = last_record_was_tool_use and bool(pending_tools)
    waiting = is_bash_wait or (minutes >= idle_min and no_result_wait)
    flags: List[str] = []
    if waiting:
        flags.append("WAITING")
    elif minutes >= idle_min:
        flags.append("IDLE")
    if size_mb > 1.0:
        flags.append("BIG")
    return AgentReport(
        id=path.stem,
        prompt=_prompt_line(prompts),
        models=models,
        input_tokens=totals["input"],
        cache_read=totals["cache_read"],
        cache_creation=totals["cache_creation"],
        output_tokens=totals["output"],
        size_mb=size_mb,
        mtime=mtime,
        minutes_since=minutes,
        last_tool=last_tool,
        flags=flags,
        path=str(path),
    )


def collect_reports(tasks_dirs: Optional[Sequence[str]] = None, *, idle_min: float = 10.0,
                    now: Optional[float] = None, active_only: bool = False) -> List[AgentReport]:
    current = time.time() if now is None else now
    reports = [parse_transcript(path, now=current, idle_min=idle_min) for path in discover_agent_files(tasks_dirs)]
    reports.sort(key=lambda report: (-report.mtime, report.id))
    if active_only:
        reports = [report for report in reports if report.minutes_since <= 60.0]
    return reports


def _row(report: AgentReport) -> str:
    models = ",".join(report.models) or "-"
    flags = ",".join(report.flags) or "-"
    return "\t".join([
        report.id, report.prompt, models,
        f"{report.input_tokens}/{report.cache_read}/{report.cache_creation}/{report.output_tokens}",
        f"{report.size_mb:.2f}",
        datetime.fromtimestamp(report.mtime, timezone.utc).isoformat(timespec="seconds") if report.mtime else "-",
        f"{report.minutes_since:.1f}", report.last_tool, flags,
    ])


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m cdx_agent.agent_usage", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tasks-dir", action="append", default=[], metavar="DIR")
    p.add_argument("--idle-min", type=float, default=10.0)
    p.add_argument("--active-only", action="store_true")
    p.add_argument("--json", action="store_true", dest="as_json")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        reports = collect_reports(args.tasks_dir or None, idle_min=args.idle_min, active_only=args.active_only)
    except (OSError, ValueError) as exc:
        print(f"agent_usage: {exc}", file=sys.stderr)
        return 2
    if args.as_json:
        print(json.dumps([report.to_dict() for report in reports], sort_keys=True))
        return 0
    print("id\tprompt\tmodel(s)\tinput/cache_read/cache_creation/output\tMB\tlast_activity\tminutes_since\tlast_tool\tflags")
    for report in reports:
        print(_row(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
