"""Read-only repo hygiene + stale-checkout helpers.

Nothing here deletes or moves anything; `report` only *suggests* commands.

- Stale-checkout detection: another git worktree of the same repo has a
  different AGENTS.md / CLAUDE.md than the checkout being launched (or a
  newer commit touching them).
- Hygiene report: removable (clean + merged) worktrees, worktrees registered
  outside the repo or prunable, oversized Claude auto-memory.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import repo_root

RULE_FILES = ("AGENTS.md", "CLAUDE.md")
MEMORY_MAX_BYTES = 8 * 1024
MEMORY_MAX_FILES = 40


def _git(cwd: Path, *args: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


@dataclass(frozen=True)
class Worktree:
    path: Path
    head: str = ""
    branch: str = ""  # short name, "" when detached
    prunable: bool = False
    bare: bool = False


def list_worktrees(repo: Path) -> list[Worktree]:
    out = _git(repo, "worktree", "list", "--porcelain")
    if not out:
        return []
    items: list[Worktree] = []
    cur: dict = {}

    def flush() -> None:
        if cur.get("worktree"):
            items.append(
                Worktree(
                    path=Path(cur["worktree"]),
                    head=cur.get("HEAD", ""),
                    branch=cur.get("branch", "").removeprefix("refs/heads/"),
                    prunable=bool(cur.get("prunable")),
                    bare=bool(cur.get("bare")),
                )
            )

    for line in out.splitlines():
        if not line.strip():
            flush()
            cur = {}
            continue
        key, _, val = line.partition(" ")
        cur[key] = val or True
    flush()
    return items


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _commit_time(wt: Path, name: str) -> int:
    out = _git(wt, "log", "-1", "--format=%ct", "--", name)
    try:
        return int((out or "").strip() or 0)
    except ValueError:
        return 0


def _rule_time(wt: Path, name: str) -> float:
    """Newest of the file's last commit time and its mtime (catches uncommitted edits)."""
    f = wt / name
    mtime = f.stat().st_mtime if f.is_file() else 0.0
    return max(float(_commit_time(wt, name)), mtime)


def stale_checkout_worktrees(repo: Path) -> list[str]:
    """Names (``branch`` or dir name) of other worktrees whose AGENTS.md/CLAUDE.md
    differ from `repo`'s AND are newer (last commit or mtime). Older copies, e.g. old
    branches, are ignored so the launch note only fires when the live rules are elsewhere."""
    repo = repo.resolve()
    found: list[str] = []
    for wt in list_worktrees(repo):
        if wt.bare or wt.prunable or wt.path.resolve() == repo or not wt.path.is_dir():
            continue
        differing = [n for n in RULE_FILES if _read(repo / n) != _read(wt.path / n)]
        if any(_rule_time(wt.path, n) > _rule_time(repo, n) for n in differing):
            found.append(wt.branch or wt.path.name)
    return found


def stale_warning_line(names: list[str]) -> str:
    return (
        "NOTE: this checkout's AGENTS.md/CLAUDE.md differ from worktree(s): "
        + ", ".join(names)
        + ". Their copies are newer: the live project rules are probably there; read them from that worktree."
    )


def resolve_worktree(repo: Path, name: str) -> Path:
    """`<repo>/.claude/worktrees/NAME` (the Claude Code worktree location)."""
    path = repo_root(repo) / ".claude" / "worktrees" / name
    if not path.is_dir():
        raise ValueError(f"--worktree {name}: no such worktree dir {path}")
    return path


# --- hygiene report ---------------------------------------------------------------


def sanitize_project_path(path: Path) -> str:
    return re.sub(r"[/.]", "-", str(path))


def memory_dir_for(repo: Path, claude_home: Path | None = None) -> Path:
    home = claude_home or (Path.home() / ".claude")
    return home / "projects" / sanitize_project_path(repo.resolve()) / "memory"


@dataclass
class HygieneReport:
    repo: Path
    removable: list[tuple[Worktree, str]] = field(default_factory=list)  # (wt, merged_into)
    outside: list[Worktree] = field(default_factory=list)
    prunable: list[Worktree] = field(default_factory=list)
    memory_bytes: int | None = None
    memory_files: int | None = None
    memory_over_size: bool = False
    memory_over_files: bool = False
    memory_dir: Path | None = None

    def flagged(self) -> int:
        return (
            len(self.removable)
            + len(self.outside)
            + len(self.prunable)
            + int(self.memory_over_size)
            + int(self.memory_over_files)
        )


def contained_in_other_branch(repo: Path, head: str, own_branch: str = "") -> str:
    """First branch (other than `own_branch`) that contains commit `head`, else ""."""
    if not head:
        return ""
    out = _git(repo, "branch", "--format=%(refname:short)", "--contains", head)
    others = [b.strip() for b in (out or "").splitlines() if b.strip() and b.strip() != own_branch]
    return others[0] if others else ""


def _merged_into(repo: Path, wt: Worktree) -> str:
    """A worktree branch counts as merged only if its HEAD commit is contained in another branch."""
    if not wt.branch:
        return ""
    return contained_in_other_branch(repo, wt.head, wt.branch)


def build_report(
    repo: Path,
    claude_home: Path | None = None,
    memory_max_bytes: int = MEMORY_MAX_BYTES,
    memory_max_files: int = MEMORY_MAX_FILES,
) -> HygieneReport:
    repo = repo_root(repo)
    rep = HygieneReport(repo=repo)
    main = list_worktrees(repo)[:1]
    main_path = main[0].path.resolve() if main else repo
    for wt in list_worktrees(repo):
        if wt.bare or wt.path.resolve() == main_path:
            continue
        if wt.prunable or not wt.path.exists():
            rep.prunable.append(wt)
            continue
        try:
            wt.path.resolve().relative_to(main_path)
        except ValueError:
            rep.outside.append(wt)
        status = _git(wt.path, "status", "--porcelain")
        if status is not None and not status.strip():
            merged = _merged_into(repo, wt)
            if merged:
                rep.removable.append((wt, merged))
    mdir = memory_dir_for(main_path, claude_home)
    rep.memory_dir = mdir
    if mdir.is_dir():
        idx = mdir / "MEMORY.md"
        rep.memory_bytes = idx.stat().st_size if idx.is_file() else 0
        rep.memory_files = sum(1 for p in mdir.iterdir() if p.is_file())
        rep.memory_over_size = rep.memory_bytes > memory_max_bytes
        rep.memory_over_files = rep.memory_files > memory_max_files
    return rep


def summary_line(rep: HygieneReport) -> str:
    parts = []
    if rep.removable:
        parts.append(f"{len(rep.removable)} removable worktree(s)")
    if rep.outside:
        parts.append(f"{len(rep.outside)} worktree(s) outside the repo")
    if rep.prunable:
        parts.append(f"{len(rep.prunable)} prunable worktree(s)")
    if rep.memory_over_size:
        parts.append(f"MEMORY.md {rep.memory_bytes // 1024} KB")
    if rep.memory_over_files:
        parts.append(f"{rep.memory_files} memory files")
    return ("hygiene: " + "; ".join(parts) + " -- run `cdx-agent --hygiene`") if parts else ""


def format_report(rep: HygieneReport, memory_max_bytes: int = MEMORY_MAX_BYTES, memory_max_files: int = MEMORY_MAX_FILES) -> str:
    L = [f"hygiene report for {rep.repo} (read-only; nothing is changed)"]
    L.append(f"\n[a] clean worktrees merged into another local branch: {len(rep.removable)}")
    for wt, merged in rep.removable:
        L.append(f"  {wt.path}  [{wt.branch}] merged into {merged}")
        L.append(f"    suggest: git -C {rep.repo} worktree remove {wt.path}")
    L.append(f"\n[b] worktrees outside the repo: {len(rep.outside)}; prunable/missing: {len(rep.prunable)}")
    for wt in rep.outside:
        L.append(f"  outside: {wt.path}  [{wt.branch or 'detached ' + wt.head[:7]}]")
        L.append(f"    suggest: git -C {rep.repo} worktree remove {wt.path}   (after checking it is clean)")
    for wt in rep.prunable:
        L.append(f"  prunable: {wt.path}")
    if rep.prunable:
        L.append(f"    suggest: git -C {rep.repo} worktree prune --dry-run   (then without --dry-run)")
    L.append(f"\n[c] Claude auto-memory: {rep.memory_dir}")
    if rep.memory_bytes is None:
        L.append("  no memory dir found")
    else:
        L.append(
            f"  MEMORY.md {rep.memory_bytes} bytes (limit {memory_max_bytes}){' OVER' if rep.memory_over_size else ''}; "
            f"{rep.memory_files} files (limit {memory_max_files}){' OVER' if rep.memory_over_files else ''}"
        )
        if rep.memory_over_size or rep.memory_over_files:
            L.append("  suggest: move superseded entries to a memory/archive/ file and shorten the index lines (manual).")
    return "\n".join(L)


def command_hygiene(args) -> int:
    rep = build_report(
        Path(args.repo),
        memory_max_bytes=args.memory_max_bytes,
        memory_max_files=args.memory_max_files,
    )
    print(format_report(rep, args.memory_max_bytes, args.memory_max_files))
    return 0


def hygiene_parser(mode: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=f"cdx-agent {mode}", description="Read-only repo hygiene check.")
    p.add_argument("--repo", default=".", help="Repository root or current directory.")
    p.add_argument("--memory-max-bytes", type=int, default=MEMORY_MAX_BYTES)
    p.add_argument("--memory-max-files", type=int, default=MEMORY_MAX_FILES)
    return p
