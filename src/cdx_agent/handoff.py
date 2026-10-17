"""Deterministic, machine-written handoff for restarting a long session.

    python -m cdx_agent.handoff [--repo DIR] [--out FILE] [--base main]
        [--branches GLOB] [--since-hours 24] [--slurm-host HOST]
        [--notes FILE] [--max-tokens 4000] [--no-slurm] [--no-gpu]

The output is engine-neutral Markdown.  Git-derived sections are deterministic
for a fixed repository state; the Generated line is the only timestamped line.
Subprocess failures are reported in their section and never abort the file.
"""
from __future__ import annotations

import argparse
import csv
import datetime as _datetime
import fnmatch
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from cdx_agent import brief


CHARS_PER_TOKEN = 4
MAX_WORKTREES = 25
MAX_REPORTS_PER_BRANCH = 3
MAX_STATUS_FILES = 3
MAX_NOTES_CHARS = 1500
SECTION_LINE_CAP = 80
JOB_ID = re.compile(r"^\d+(?:[_.][A-Za-z0-9]+)?$")
HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
OPEN_HEADING = re.compile(r"\b(todo|open|next|pending|left)\b", re.IGNORECASE)


class HandoffError(Exception):
    """An expected handoff-generation failure."""


class CommandError(HandoffError):
    """A subprocess failed without exposing its output to the handoff."""


def _run_command(command: Sequence[str], timeout: int = 30) -> str:
    """Run one command with captured output and a hard timeout."""
    env = dict(os.environ, LC_ALL="C", GIT_PAGER="cat")
    try:
        result = subprocess.run(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise CommandError("timed out")
    except OSError:
        raise CommandError("not available")
    if result.returncode != 0:
        raise CommandError("command failed")
    return result.stdout


def _git(repo: str, *args: str, check: bool = True) -> str:
    try:
        return _run_command(["git", "-c", "core.quotepath=off", "-C", repo, *args], timeout=60).rstrip("\n")
    except CommandError:
        if check:
            raise HandoffError("git unavailable")
        return ""


def _clip(text: str, limit: int = 200) -> str:
    text = text.replace("\x00", "").replace("\r", "")
    if len(text) <= limit:
        return text
    return text[: max(limit - 3, 0)] + "..."


def _unsafe_name(path: str) -> bool:
    """Avoid reading paths whose names conventionally carry secrets."""
    name = Path(path).name.lower()
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in ("*key*", "*secret*", "*token*", "*.env"))


def _repo_root(repo: str) -> str:
    root = _git(repo, "rev-parse", "--show-toplevel", check=False)
    if not root:
        raise HandoffError(f"{repo}: not a git repository")
    return str(Path(root).resolve())


def _current_branch(repo: str) -> str:
    return _git(repo, "symbolic-ref", "--short", "-q", "HEAD", check=False) or "(detached HEAD)"


def _head(repo: str) -> str:
    sha = _git(repo, "rev-parse", "--short", "HEAD", check=False) or "?"
    subject = _git(repo, "log", "-1", "--format=%s", "HEAD", check=False)
    return _clip(f"{sha} {subject}".rstrip(), 200)


def _parse_worktrees(text: str) -> List[Tuple[str, str, str]]:
    """Return (path, branch, full HEAD) records from porcelain output."""
    records: List[Tuple[str, str, str]] = []
    path = branch = head = ""

    def flush() -> None:
        nonlocal path, branch, head
        if path and branch:
            records.append((path, branch, head))
        path = branch = head = ""

    for line in text.splitlines() + [""]:
        if not line.strip():
            flush()
        elif line.startswith("worktree "):
            if path:
                flush()
            path = line[len("worktree "):]
        elif line.startswith("HEAD "):
            head = line[len("HEAD "):].strip()
        elif line.startswith("branch refs/heads/"):
            branch = line[len("branch refs/heads/"):].strip()
    return records


def _base_name(base: str) -> str:
    prefix = "refs/heads/"
    return base[len(prefix):] if base.startswith(prefix) else base


def _ahead_behind(repo: str, base: str, branch: str) -> Tuple[str, str]:
    parts = _git(repo, "rev-list", "--left-right", "--count", f"{base}...{branch}", check=False).split()
    if len(parts) == 2 and all(part.isdigit() for part in parts):
        return parts[1], parts[0]
    return "?", "?"


def _dirty_count(worktree: str) -> str:
    try:
        status = _git(worktree, "status", "--porcelain", "-uno")
    except HandoffError:
        return "?"
    return str(len([line for line in status.splitlines() if line.strip()]))


def _worktree_details(repo: str, base: str, branches: str) -> List[str]:
    porcelain = _git(repo, "worktree", "list", "--porcelain", check=False)
    records = _parse_worktrees(porcelain)
    base_branch = _base_name(base)
    details: List[Tuple[int, str, str]] = []
    for worktree, branch, full_head in records:
        if branch == base_branch or not fnmatch.fnmatchcase(branch, branches):
            continue
        stamp_text = _git(repo, "log", "-1", "--format=%ct", branch, check=False)
        try:
            stamp = int(stamp_text)
        except ValueError:
            stamp = 0
        sha = _git(repo, "rev-parse", "--short", branch, check=False) or (full_head[:7] if full_head else "?")
        subject = _git(repo, "log", "-1", "--format=%s", branch, check=False)
        ahead, behind = _ahead_behind(repo, base, branch)
        dirty = _dirty_count(worktree)
        line = f"- {branch}: {sha} {_clip(subject, 70)} (ahead {ahead}, behind {behind}, dirty {dirty})"
        details.append((stamp, branch, line))
    details.sort(key=lambda item: (-item[0], item[1]))
    lines = [item[2] for item in details[:MAX_WORKTREES]]
    return lines or ["(none)"]


def _changed_paths(repo: str, base: str, branch: str) -> List[str]:
    text = _git(repo, "diff", "--name-only", "--diff-filter=d", "--no-renames", f"{base}...{branch}", check=False)
    return sorted(path for path in text.splitlines() if path)


def _report_lines(repo: str, base: str, records: List[Tuple[str, str, str]], since_hours: float) -> List[str]:
    cutoff = time.time() - max(since_hours, 0.0) * 3600.0
    lines: List[str] = []
    for worktree, branch, _ in records:
        reports: List[Tuple[str, List[str]]] = []
        for path in _changed_paths(repo, base, branch):
            if not path.lower().startswith("reports/") or not path.lower().endswith(".md") or _unsafe_name(path):
                continue
            report = Path(worktree) / path
            try:
                if not report.is_file() or report.stat().st_mtime < cutoff:
                    continue
                headline = brief.report_headline(report.read_text(errors="replace"))
            except OSError:
                continue
            reports.append((path, headline))
            if len(reports) >= MAX_REPORTS_PER_BRANCH:
                break
        lines.append(f"- {branch}:")
        if not reports:
            lines.append("  (none)")
            continue
        for path, headline in reports:
            lines.append(f"  - {path}: {_clip(headline[0])}")
            lines.extend(f"    {_clip(row)}" for row in headline[1:])
    return lines or ["(none)"]


def _extract_open_sections(text: str) -> List[Tuple[str, List[str]]]:
    lines = text.splitlines()
    sections: List[Tuple[str, List[str]]] = []
    current: Optional[Tuple[int, str, List[str]]] = None
    in_fence = False
    for line in lines:
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
        match = None if in_fence else HEADING.match(line)
        if match:
            level = len(match.group(1))
            if current and level <= current[0]:
                sections.append((current[1], current[2]))
                current = None
            if current is None and OPEN_HEADING.search(match.group(2)):
                current = (level, line.strip(), [])
                continue
        if current is not None and line.strip():
            current[2].append(_clip(line.rstrip()))
    if current:
        sections.append((current[1], current[2]))
    return sections


def _status_files(repo: str, output_path: Optional[Path]) -> List[Path]:
    status_dir = Path(repo) / "docs" / "status"
    paths: Dict[str, Path] = {}
    for pattern in ("*handoff*.md", "*todo*.md", "*plan*.md"):
        for path in status_dir.glob(pattern):
            if not path.is_file() or _unsafe_name(path.name) or "handoff_auto" in path.name.lower():
                continue
            if output_path is not None:
                try:
                    if path.resolve() == output_path.resolve():
                        continue
                except OSError:
                    pass
            paths[str(path)] = path
    ranked: List[Tuple[float, str, Path]] = []
    for path in paths.values():
        try:
            ranked.append((path.stat().st_mtime, str(path), path))
        except OSError:
            pass
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in ranked[:MAX_STATUS_FILES]]


def _open_item_lines(repo: str, output_path: Optional[Path]) -> List[str]:
    lines: List[str] = []
    for path in _status_files(repo, output_path):
        try:
            sections = _extract_open_sections(path.read_text(errors="replace"))
        except OSError:
            continue
        if not sections:
            continue
        lines.append(f"- {path.relative_to(repo)}")
        for heading, body in sections:
            lines.append(f"  {heading}")
            lines.extend(f"  {_clip(line)}" for line in body[:SECTION_LINE_CAP])
            if len(body) > SECTION_LINE_CAP:
                lines.append(f"  ... (+{len(body) - SECTION_LINE_CAP} more lines)")
    return lines or ["(none)"]


def _valid_job_line(line: str, drop_states: bool = False) -> bool:
    fields = line.split()
    if not fields or not JOB_ID.match(fields[0]):
        return False
    if drop_states and len(fields) >= 3 and fields[2].upper().split("+")[0] in {"RUNNING", "PENDING"}:
        return False
    return True


def _job_lines(text: str, drop_states: bool = False) -> List[str]:
    return [line.strip() for line in text.splitlines() if _valid_job_line(line, drop_states)]


def _remote_command(host: str, command: str) -> List[str]:
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, command]


def _slurm_lines(no_slurm: bool, slurm_host: Optional[str], since_hours: float) -> Optional[List[str]]:
    host = slurm_host or os.environ.get("CDX_SLURM_HOST")
    if no_slurm or (not host and not shutil.which("squeue")):
        return None
    user = os.environ.get("USER", "")
    hours_value = float(since_hours)
    hours = str(int(hours_value)) if hours_value.is_integer() else str(hours_value)
    squeue = ["squeue", "-u", user, "-h", "-o", "%i %j %T %M %N %r"]
    sacct = ["sacct", "-u", user, "-S", f"now-{hours}hours", "-X", "-n",
             "-o", "JobID,JobName%24,State%12,Elapsed,NodeList%10"]
    if host:
        squeue = _remote_command(host, 'squeue -u "$USER" -h -o "%i %j %T %M %N %r"')
        sacct = _remote_command(host, f'sacct -u "$USER" -S now-{hours}hours -X -n -o "JobID,JobName%24,State%12,Elapsed,NodeList%10"')
    errors: List[str] = []
    queue_text = finished_text = ""
    try:
        queue_text = _run_command(squeue, timeout=30)
    except HandoffError as exc:
        errors.append(f"squeue {exc}")
    try:
        finished_text = _run_command(sacct, timeout=30)
    except HandoffError as exc:
        errors.append(f"sacct {exc}")
    queue = _job_lines(queue_text)
    finished = _job_lines(finished_text, drop_states=True)[-25:]
    lines: List[str] = []
    if queue:
        lines.append("queued/running:")
        lines.extend(f"  {line}" for line in queue)
    elif not errors:
        lines.append("queued/running: (none)")
    if finished:
        lines.append("finished:")
        lines.extend(f"  {line}" for line in finished)
    elif not errors:
        lines.append("finished: (none)")
    if errors:
        lines.append(f"(unavailable: {'; '.join(errors)})")
    return lines or ["(none)"]


def _csv_fields(line: str) -> List[str]:
    try:
        return [field.strip() for field in next(csv.reader([line]))]
    except (StopIteration, csv.Error):
        return []


def _gpu_lines(no_gpu: bool) -> Optional[List[str]]:
    if no_gpu or not shutil.which("nvidia-smi"):
        return None
    try:
        gpu_text = _run_command(
            ["nvidia-smi", "--query-gpu=index,uuid,memory.used,utilization.gpu", "--format=csv,noheader,nounits"],
            timeout=30,
        )
        apps_text = _run_command(
            ["nvidia-smi", "--query-compute-apps=gpu_uuid,pid", "--format=csv,noheader,nounits"],
            timeout=30,
        )
    except HandoffError as exc:
        return [f"(unavailable: {exc})"]

    owners: Dict[str, List[str]] = {}
    for line in apps_text.splitlines():
        fields = _csv_fields(line)
        if len(fields) < 2:
            continue
        uuid, pid = fields[0], fields[1]
        try:
            user = _run_command(["ps", "-o", "user=", "-p", pid], timeout=10).strip()
        except HandoffError:
            continue
        if user:
            owners.setdefault(uuid, []).append(user.splitlines()[0].strip())

    lines: List[str] = []
    for line in gpu_text.splitlines():
        fields = _csv_fields(line)
        if len(fields) >= 4:
            index, uuid, memory, utilization = fields[:4]
        elif len(fields) == 3:
            index, memory, utilization = fields
            uuid = index
        else:
            continue
        names = sorted(set(name for name in owners.get(uuid, []) if name))
        lines.append(f"- GPU {index}: {memory} MiB, {utilization}% util, owners: {', '.join(names) if names else '(none)'}")
    return lines or ["(none)"]


def _tmux_lines() -> List[str]:
    if not shutil.which("tmux"):
        return ["(unavailable: tmux not available)"]
    try:
        text = _run_command(["tmux", "ls", "-F", "#{session_name}"], timeout=10)
    except HandoffError as exc:
        return [f"(unavailable: {exc})"]
    names = sorted(set(line.strip() for line in text.splitlines() if line.strip()))
    return [f"- {name}" for name in names] or ["(none)"]


def _notes_lines(notes_path: Optional[str]) -> List[str]:
    if not notes_path or _unsafe_name(notes_path):
        return ["(none)"]
    try:
        text = Path(notes_path).read_text(errors="replace")[:MAX_NOTES_CHARS]
    except OSError:
        return ["(unavailable: notes file unreadable)"]
    return text.splitlines() or ["(none)"]


def _rules_path() -> Path:
    return brief.default_rules_path().parent / "AGENTS.md"


def _section(title: str, lines: Iterable[str]) -> str:
    body = list(lines)
    if len(body) > SECTION_LINE_CAP:
        body = body[: SECTION_LINE_CAP - 1] + [f"... (+{len(body) - SECTION_LINE_CAP + 1} more lines)"]
    return "\n".join([title, *body]) + "\n"


def _cap_output(text: str, max_tokens: int) -> str:
    budget = max(max_tokens, 1) * CHARS_PER_TOKEN
    if len(text) <= budget:
        return text
    marker = f"... (handoff truncated at --max-tokens {max_tokens})\n"
    if len(marker) >= budget:
        return marker[:budget]
    kept = text[: budget - len(marker)]
    return kept.rsplit("\n", 1)[0] + "\n" + marker


# Trimmed first when over budget; the header, notes and resume sections are never trimmed.
TRIM_ORDER = ("## Reports", "## Open items", "## Slurm", "## Worktrees", "## Local GPUs", "## tmux sessions")


def _trim_section(text: str, keep_chars: int, marker: str) -> str:
    title = text.split("\n", 1)[0]
    room = keep_chars - len(marker)
    if room <= len(title) + 1:
        return title + "\n" + marker
    kept = text[:room].rsplit("\n", 1)[0]
    return (kept if kept else title) + "\n" + marker


def _cap_sections(sections: Sequence[str], max_tokens: int) -> str:
    """Fit ``sections`` into ``max_tokens``, trimming the bulky sections (TRIM_ORDER) from their tails first,
    so the header, the agent notes and the resume line always survive."""
    budget = max(max_tokens, 1) * CHARS_PER_TOKEN
    parts = list(sections)
    marker = f"... (section trimmed to fit --max-tokens {max_tokens})\n"
    for title in TRIM_ORDER:
        excess = sum(map(len, parts)) - budget
        if excess <= 0:
            break
        for i, part in enumerate(parts):
            if part.startswith(title + "\n"):
                parts[i] = _trim_section(part, max(len(part) - excess, 0), marker)
                break
    return _cap_output("".join(parts), max_tokens)


def build_handoff(repo: str, base: str = "main", branches: str = "*", since_hours: float = 24.0,
                  slurm_host: Optional[str] = None, notes_path: Optional[str] = None,
                  max_tokens: int = 4000, no_slurm: bool = False, no_gpu: bool = False,
                  output_path: Optional[str] = None, generated: Optional[str] = None) -> str:
    """Build a handoff document for ``repo``."""
    root = _repo_root(repo)
    out_path = Path(output_path).resolve() if output_path else None
    worktree_text = _git(root, "worktree", "list", "--porcelain", check=False)
    records = [record for record in _parse_worktrees(worktree_text)
               if record[1] != _base_name(base) and fnmatch.fnmatchcase(record[1], branches)]
    # Worktree metadata is collected once for the details and report sections.
    worktree_lines = _worktree_details(root, base, branches)

    current = _current_branch(root)
    generated = generated or _datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    sections = [
        "\n".join([
            f"# Handoff {Path(root).name}",
            f"Generated: {generated}",
            f"Repo root: {root}",
            f"Current: {current} @ {_head(root)}",
        ]) + "\n",
        _section("## Worktrees", worktree_lines),
    ]
    slurm = _slurm_lines(no_slurm, slurm_host, since_hours)
    if slurm is not None:
        sections.append(_section("## Slurm", slurm))
    gpu = _gpu_lines(no_gpu)
    if gpu is not None:
        sections.append(_section("## Local GPUs", gpu))
    sections.extend([
        _section("## tmux sessions", _tmux_lines()),
        _section("## Reports", _report_lines(root, base, records, since_hours)),
        _section("## Open items", _open_item_lines(root, out_path)),
        _section("## Notes (agent-written interpretation)", _notes_lines(notes_path)),
        _section("## Resume", [
            f"Base rules: {_rules_path()}",
            f'Start a fresh session in {root} and say: "Read {out_path or "<out path>"} and continue from its open items."',
        ]),
    ])
    return _cap_sections(sections, max_tokens)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m cdx_agent.handoff", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=".", help="repository root (default: cwd)")
    parser.add_argument("--out", default=None, help="output file (default: docs/status/YYYY-MM-DD_handoff_auto.md)")
    parser.add_argument("--base", default="main", help="base branch or revision (default: main)")
    parser.add_argument("--branches", default="*", help="fnmatch branch filter (default: all except base)")
    parser.add_argument("--since-hours", type=float, default=24.0, help="report/job lookback window (default: 24)")
    parser.add_argument("--slurm-host", default=None, help="run Slurm queries on this host")
    parser.add_argument("--notes", default=None, help="agent-written notes file")
    parser.add_argument("--max-tokens", type=int, default=4000, help="output cap at about four chars per token")
    parser.add_argument("--no-slurm", action="store_true", help="omit Slurm")
    parser.add_argument("--no-gpu", action="store_true", help="omit local GPU data")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = _repo_root(args.repo)
    except HandoffError as exc:
        print(f"handoff: {exc}", file=sys.stderr)
        return 2
    out = Path(args.out).expanduser().resolve() if args.out else Path(root) / "docs" / "status" / (
        _datetime.datetime.now().date().isoformat() + "_handoff_auto.md"
    )
    try:
        text = build_handoff(root, args.base, args.branches, args.since_hours, args.slurm_host,
                             args.notes, args.max_tokens, args.no_slurm, args.no_gpu, str(out))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
    except (HandoffError, OSError, ValueError) as exc:
        print(f"handoff: {exc}", file=sys.stderr)
        return 2
    print(f"handoff={out}")
    print(f"tokens~={(len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
