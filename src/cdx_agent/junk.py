"""Read-only junk scan (WP7) and report-driven apply.

    python -m cdx_agent.junk scan [--root DIR]... [--project P] [--json] [--no-inuse]
    python -m cdx_agent.junk apply --from report.json [--dry-run]

scan never changes anything. apply passes only rows whose action is "delete" or "trash" to trash.rm, which
re-classifies and re-checks every path itself.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from cdx_agent import delete_policy, hygiene, inuse, trash, values

WALK_SECONDS = 20.0
ACTIONS = {"a": "delete", "b": "trash", "c": "keep"}
TMP_PID_RE = re.compile(r"\.tmp\.(\d+)$")
BAK_RE = re.compile(r"^(?P<orig>.+?)\.bak(?:[._-].*)?$")
RESULT_DIR_NAMES = {"results", "outputs", "runs", "evals"}
DUP_FILE_CAP = 5000


def _bytes(path: str, limit: float = WALK_SECONDS) -> tuple[int, str]:
    """(bytes, note): os.walk sum of st_size, symlinks not followed, walk time capped."""
    try:
        if os.path.islink(path):
            return 0, ""
        if not os.path.isdir(path):
            return os.lstat(path).st_size, ""
    except OSError:
        return 0, ""
    total, end = 0, time.monotonic() + limit
    for dp, _dn, fn in os.walk(path):
        for n in fn:
            p = os.path.join(dp, n)
            try:
                if not os.path.islink(p):
                    total += os.lstat(p).st_size
            except OSError:
                pass
        if time.monotonic() > end:
            return total, f"size is a lower bound (walk capped at {limit:g}s)"
    return total, ""


def _newest_mtime(path: str, limit: float = 5.0) -> float:
    try:
        newest = os.lstat(path).st_mtime
    except OSError:
        return 0.0
    if not os.path.isdir(path) or os.path.islink(path):
        return newest
    end = time.monotonic() + limit
    for dp, dn, fn in os.walk(path):
        if ".git" in dn:
            dn.remove(".git")
        for n in fn:
            try:
                newest = max(newest, os.lstat(os.path.join(dp, n)).st_mtime)
            except OSError:
                pass
        if time.monotonic() > end:
            break
    return newest


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _git_worktrees(repo: Path) -> List[Path]:
    out = delete_policy._git_out(repo, "worktree", "list", "--porcelain")
    return [Path(ln[9:]) for ln in (out or "").splitlines() if ln.startswith("worktree ")]


def default_roots(cwd: Optional[Path] = None) -> List[Path]:
    cwd = Path(cwd or Path.cwd())
    roots: List[Path] = []
    main = delete_policy.main_checkout(cwd)
    if main and main.is_dir():
        roots.append(main)
        roots += [w for w in _git_worktrees(main) if w != main and w.is_dir()]
    tmp = Path.home() / ".tmp"
    if tmp.is_dir():
        roots.append(tmp)
    return roots


class Scanner:
    def __init__(self, roots: Sequence[Path], project: str = "", use_inuse: bool = True,
                 in_use: Optional[Callable[..., dict]] = None, repo: Optional[Path] = None):
        self.roots = [Path(r).resolve() for r in roots]
        self.project = project
        self.repo = Path(repo) if repo else Path.cwd()
        self.main = delete_policy.main_checkout(self.repo)
        self.scratch_age = float(values.get("junk.scratch_age_days", 14))
        self.bak_keep = int(values.get("junk.bak_keep", 3))
        self.wt_age = float(values.get("junk.worktree_age_days", 30))
        if in_use is None:
            def in_use(p: Any) -> dict:
                return inuse.in_use(p, local_only=True)
        self._in_use = in_use if use_inuse else None
        self._session_in_use = in_use
        self.ctx = delete_policy.default_ctx(self.repo, in_use=self._in_use)
        self.now = time.time()
        self.rows: List[Dict[str, Any]] = []
        self._seen: set[str] = set()
        self.secondary: set[str] = set()
        main_real = self.main.resolve() if self.main else None
        for r in self.roots:
            for w in _git_worktrees(r):
                if w.resolve() != main_real:
                    self.secondary.add(str(w.resolve()))

    def _age(self, path: str) -> float:
        try:
            return round((self.now - os.lstat(path).st_mtime) / 86400.0, 1)
        except OSError:
            return 0.0

    def _row(self, path: str, rule: str, project: str, reason: str = "", action: Optional[str] = None,
             age: Optional[float] = None) -> None:
        """Tier always comes from delete_policy.classify; `action` may only soften it (trash/keep/report)."""
        path = os.path.abspath(path)
        if path in self._seen:
            return
        self._seen.add(path)
        dec = delete_policy.classify(Path(path), self.ctx)
        nbytes, note = _bytes(path)
        verdict = "skipped"
        if self._in_use is not None:
            verdict = str(self._in_use(Path(path)).get("verdict", "unknown"))
        act = ACTIONS[dec.tier]
        why = f"{dec.rule_id}: {dec.reason}"
        if action in ("keep", "report"):
            act, why = action, reason
        elif action == "trash" and dec.tier != "c":
            act, why = "trash", reason
        elif reason:
            why = f"{reason}; {why}"
        if note:
            why += f" [{note}]"
        self.rows.append({"path": path, "rule": rule, "bytes": nbytes,
                          "age_days": self._age(path) if age is None else age, "project": project,
                          "inuse": verdict, "tier": dec.tier, "action": act, "reason": why})

    def scan(self) -> List[Dict[str, Any]]:
        tmp = (Path.home() / ".tmp").resolve()
        for root in self.roots:
            if root == tmp:
                self._sessions(root)
            else:
                self._walk_project(root, self.project or root.name)
        self._worktrees()
        return self.rows

    def _walk_project(self, root: Path, proj: str) -> None:
        baks: Dict[str, List[str]] = {}
        end = time.monotonic() + 60
        for dp, dn, fn in os.walk(root):
            keep = []
            for d in dn:
                full = os.path.join(dp, d)
                if d == ".git" or os.path.realpath(full) in self.secondary or os.path.islink(full):
                    continue
                if d in ("__pycache__", ".pytest_cache"):
                    self._row(full, "pycache", proj)
                    continue
                if d == ".partial":
                    self._row(full, "partial", proj, "partial output directory", action="trash")
                    continue
                keep.append(d)
            dn[:] = keep
            for d in dn:
                if d in RESULT_DIR_NAMES and "downloads" not in Path(dp).parts:
                    self._dups(os.path.join(dp, d), proj)
            for n in fn:
                full = os.path.join(dp, n)
                m = TMP_PID_RE.search(n)
                if m and not _pid_alive(int(m.group(1))):
                    self._row(full, "partial", proj, f"pid {m.group(1)} is dead", action="trash")
                    continue
                b = BAK_RE.match(n)
                if b:
                    baks.setdefault(os.path.join(dp, b.group("orig")), []).append(full)
            if time.monotonic() > end:
                break
        for files in baks.values():
            files.sort(key=lambda f: (os.lstat(f).st_mtime, f), reverse=True)
            for f in files[self.bak_keep:]:
                self._row(f, "bak", proj, f"older than newest {self.bak_keep} backups", action="trash")

    def _sessions(self, tmp: Path) -> None:
        claude = Path.home() / ".claude" / "projects"
        for top in sorted(tmp.glob("claude-*")):
            for projdir in sorted(p for p in top.iterdir() if p.is_dir()):
                for sess in sorted(p for p in projdir.iterdir() if p.is_dir()):
                    self._session(sess, projdir.name, claude / projdir.name / f"{sess.name}.jsonl")

    def _session(self, sess: Path, slug: str, transcript: Path) -> None:
        p = str(sess)
        try:
            tage: Optional[float] = (self.now - transcript.stat().st_mtime) / 86400.0
        except OSError:
            tage = None
        if tage is not None and tage <= self.scratch_age:
            self._row(p, "session_tmp", slug, f"session transcript touched {tage:.1f} days ago", action="keep")
            return
        if self._in_use is not None and self._session_in_use(sess).get("verdict") != "free":
            self._row(p, "session_tmp", slug, "live process has files or cwd under it", action="keep")
            return
        ref = self._referenced(sess.name)
        if ref:
            self._row(p, "session_tmp", slug, f"referenced by {ref}", action="keep")
            return
        why = "session transcript missing" if tage is None else f"session transcript {tage:.0f} days old"
        self._row(p, "session_tmp", slug, why, action="trash")

    def _referenced(self, name: str) -> str:
        if not self.main:
            return ""
        try:
            r = subprocess.run(["git", "-C", str(self.main), "grep", "-l", "-I", "-F", "--", name],
                               capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        files = r.stdout.split()
        return files[0] if r.returncode == 0 and files else ""

    def _worktrees(self) -> None:
        for w in sorted(self.secondary):
            if not os.path.isdir(w) or w in self._seen:
                continue
            proj = self.project or Path(w).name
            head = hygiene._git(Path(w), "log", "-1", "--format=%ct")
            ctime = float(head.strip()) if head and head.strip().isdigit() else 0.0
            age = round((self.now - max(ctime, _newest_mtime(w))) / 86400.0, 1)
            dec = delete_policy.classify(Path(w), self.ctx)
            if dec.tier in ("a", "c"):
                self._row(w, "agent_worktree", proj, age=age)
            elif age > self.wt_age:
                self._row(w, "agent_worktree", proj, f"unmerged or dirty, idle {age:.0f} days",
                          action="trash", age=age)
            else:
                self._row(w, "agent_worktree", proj,
                          f"unmerged or dirty, idle {age:.0f} days (limit {self.wt_age:g})", action="keep", age=age)

    def _dups(self, base: str, proj: str) -> None:
        groups: Dict[frozenset, List[str]] = {}
        try:
            kids = sorted(e.path for e in os.scandir(base) if e.is_dir(follow_symlinks=False))
        except OSError:
            return
        for kid in kids:
            sig, count = set(), 0
            for dp, _dn, fn in os.walk(kid):
                for n in fn:
                    p = os.path.join(dp, n)
                    count += 1
                    if not os.path.islink(p):
                        sig.add((os.path.relpath(p, kid), os.lstat(p).st_size))
                if count > DUP_FILE_CAP:
                    break
            if sig and count <= DUP_FILE_CAP:
                groups.setdefault(frozenset(sig), []).append(kid)
        for members in groups.values():
            for dup in members[1:]:
                self._row(dup, "duplicate_tree", proj, f"same (path, size) set as {members[0]}", action="report")


def scan(roots: Optional[Sequence[Path]] = None, project: str = "", use_inuse: bool = True,
         in_use: Optional[Callable[..., dict]] = None, repo: Optional[Path] = None) -> Dict[str, Any]:
    roots = list(roots) if roots else default_roots(repo)
    rows = Scanner(roots, project, use_inuse, in_use, repo).scan()
    totals: Dict[str, Dict[str, int]] = {}
    for r in rows:
        t = totals.setdefault(r["rule"], {"count": 0, "bytes": 0})
        t["count"] += 1
        t["bytes"] += r["bytes"]
    return {"rows": rows, "totals": totals}


def apply(report: str, dry_run: bool = False) -> int:
    data = json.loads(Path(report).read_text(encoding="utf-8"))
    rows = data["rows"] if isinstance(data, dict) else data
    paths = [r["path"] for r in rows
             if isinstance(r, dict) and r.get("action") in ("delete", "trash") and r.get("path")]
    return trash.rm(paths, dry_run=dry_run) if paths else 0


def _human(n: int) -> str:
    x = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if x < 1024:
            return f"{x:.0f}{unit}"
        x /= 1024
    return f"{x:.1f}TB"


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="cdx_agent.junk")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--root", action="append", default=[])
    s.add_argument("--project", default="")
    s.add_argument("--json", action="store_true")
    s.add_argument("--no-inuse", action="store_true")
    a = sub.add_parser("apply")
    a.add_argument("--from", dest="report", required=True)
    a.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "apply":
        return apply(args.report, args.dry_run)
    result = scan([Path(r) for r in args.root], args.project, not args.no_inuse)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    for r in result["rows"]:
        print(f"{r['action']:<6} {r['tier']} {r['rule']:<15} {_human(r['bytes']):>7} {r['age_days']:>6}d  "
              f"{r['path']}  # {r['reason']}")
    for rule, t in sorted(result["totals"].items()):
        print(f"total {rule}: {t['count']} rows, {_human(t['bytes'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
