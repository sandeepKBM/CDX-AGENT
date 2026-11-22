"""cdx-agent --deploy: install the git-tracked tool sources into their live paths.

Sources live in git (decision Q7, 2026-11-22):
  CDX-AGENT/tools/   (tracked)       bin/cdx-agent, hooks/*.py
  CDX-AGENT/site/    (own local git, gitignored by CDX-AGENT, no remote)
                     base/* (rules, policies, ledgers), skills/shared/*, skills/user/*

Live paths are unchanged (runtimes symlink to them), so a deploy is:
  1. both trees clean in every deployed file (unless --allow-dirty),
  2. the test suite passes (unless --skip-tests),
  3. no live file drifted from what was last deployed (unless --force),
  4. per file: write a temp file beside the live path, then os.replace (atomic on one filesystem),
  5. record {git_sha, site_sha, files: {live: sha256}} in <codex_tools>/.deployed.json and append it to
     <codex_tools>/.deploy_log.jsonl.

Files that exist live but not in the repo are reported, never deleted (deleting goes through `cdx-agent --rm`).
`--rev SHA [--site-rev SHA]` deploys an older commit (rollback): file contents come from `git show`.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_USER_ROOT = Path(os.environ.get("CDX_USER_ROOT", "/common/users/ss5772"))


@dataclass(frozen=True)
class Target:
    tree: str       # "tools" (outer repo) or "site" (inner repo)
    rel: str        # path relative to the tree root
    live: Path      # live path


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> Optional[str]:
    try:
        return sha256_bytes(path.read_bytes())
    except (FileNotFoundError, IsADirectoryError):
        return None


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {cwd}: {p.stderr.strip()}")
    return p.stdout


def _git_bytes(cwd: Path, *args: str) -> bytes:
    p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {cwd}: {p.stderr.decode(errors='replace').strip()}")
    return p.stdout


def build_map(repo: Path, user_root: Path, home: Path) -> list[Target]:
    """Every deployable file, from the files tracked at HEAD of each tree."""
    tools_root = user_root / "codex_tools"
    out: list[Target] = []
    for rel in _git(repo, "ls-files", "tools").splitlines():
        sub = rel[len("tools/"):]
        if sub == "bin/cdx-agent":
            out.append(Target("tools", sub, user_root / "bin" / "cdx-agent"))
        elif sub.startswith("hooks/") and sub.count("/") == 1:
            out.append(Target("tools", sub, tools_root / sub))
    site = repo / "site"
    if (site / ".git").exists():
        for sub in _git(site, "ls-files").splitlines():
            if sub == "base/repo.AGENTS.md":
                out.append(Target("site", sub, tools_root / "templates" / "repo.AGENTS.md"))
            elif sub.startswith("base/"):
                out.append(Target("site", sub, tools_root / sub))
            elif sub.startswith("skills/shared/"):
                out.append(Target("site", sub, tools_root / "skills" / sub[len("skills/shared/"):]))
            elif sub.startswith("skills/user/"):
                out.append(Target("site", sub, home / ".agents" / "skills" / sub[len("skills/user/"):]))
    return out


def tree_root(repo: Path, tree: str) -> Path:
    return repo / "tools" if tree == "tools" else repo / "site"


def dirty_files(repo: Path, targets: Iterable[Target]) -> list[str]:
    wanted = {(t.tree, t.rel) for t in targets}
    dirty: list[str] = []
    for line in _git(repo, "status", "--porcelain", "--", "tools").splitlines():
        rel = line[3:].strip()
        if rel.startswith("tools/") and ("tools", rel[len("tools/"):]) in wanted:
            dirty.append(rel)
    site = repo / "site"
    if (site / ".git").exists():
        for line in _git(site, "status", "--porcelain").splitlines():
            rel = line[3:].strip()
            if ("site", rel) in wanted:
                dirty.append("site/" + rel)
    return dirty


def content_at(repo: Path, t: Target, rev: Optional[str]) -> bytes:
    root = tree_root(repo, t.tree)
    if rev is None:
        return (root / t.rel).read_bytes()
    git_cwd = repo if t.tree == "tools" else root
    git_path = ("tools/" + t.rel) if t.tree == "tools" else t.rel
    return _git_bytes(git_cwd, "show", f"{rev}:{git_path}")


def first_added_sha(repo: Path, t: Target) -> Optional[str]:
    """sha256 of the file as first committed (the import of the live copy), used as the drift baseline
    before any deploy record exists."""
    git_cwd = repo if t.tree == "tools" else tree_root(repo, t.tree)
    git_path = ("tools/" + t.rel) if t.tree == "tools" else t.rel
    revs = _git(git_cwd, "log", "--diff-filter=A", "--format=%H", "--", git_path, check=False).split()
    if not revs:
        return None
    try:
        return sha256_bytes(_git_bytes(git_cwd, "show", f"{revs[-1]}:{git_path}"))
    except RuntimeError:
        return None


def load_record(record_path: Path) -> dict:
    try:
        return json.loads(record_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def atomic_install(data: bytes, live: Path, mode: int) -> None:
    live.parent.mkdir(parents=True, exist_ok=True)
    tmp = live.parent / f".{live.name}.cdxdeploy.{os.getpid()}"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, live)


def run_tests(repo: Path) -> int:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
                       cwd=repo, env=env, capture_output=True, text=True)
    tail = "\n".join((p.stdout + p.stderr).strip().splitlines()[-15:])
    print(tail)
    return p.returncode


@dataclass
class Plan:
    install: list[tuple[Target, bytes]]
    same: list[Target]
    drift: list[tuple[Target, str]]


def make_plan(repo: Path, targets: list[Target], record: dict, rev: Optional[str], site_rev: Optional[str]) -> Plan:
    files_rec = record.get("files", {})
    plan = Plan([], [], [])
    for t in targets:
        data = content_at(repo, t, rev if t.tree == "tools" else site_rev)
        new_sha = sha256_bytes(data)
        live_sha = sha256_file(t.live)
        if live_sha == new_sha:
            plan.same.append(t)
            continue
        if live_sha is not None:
            baseline = files_rec.get(str(t.live)) or first_added_sha(repo, t)
            if baseline is not None and live_sha != baseline:
                plan.drift.append((t, live_sha))
        plan.install.append((t, data))
    return plan


def live_only(targets: list[Target], user_root: Path, home: Path) -> list[Path]:
    """Live files under the skill roots that the repo does not carry (retired or never imported)."""
    known = {t.live for t in targets}
    roots = [user_root / "codex_tools" / "skills", home / ".agents" / "skills"]
    extra: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if p.is_file() and ".bak" not in p.name and "__pycache__" not in p.parts and p not in known:
                extra.append(p)
    return sorted(extra)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="cdx-agent --deploy", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print what would change; touch nothing")
    ap.add_argument("--repo", type=Path, default=REPO_ROOT)
    ap.add_argument("--user-root", type=Path, default=DEFAULT_USER_ROOT)
    ap.add_argument("--home", type=Path, default=Path.home())
    ap.add_argument("--rev", default=None, help="deploy tools/ from this CDX-AGENT commit (rollback)")
    ap.add_argument("--site-rev", default=None, help="deploy site/ from this site commit (rollback)")
    ap.add_argument("--only", action="append", default=[], help="limit to repo paths starting with this prefix")
    ap.add_argument("--allow-dirty", action="store_true", help="deploy uncommitted working-tree files")
    ap.add_argument("--skip-tests", action="store_true")
    ap.add_argument("--force", action="store_true", help="overwrite live files that drifted from the last deploy")
    a = ap.parse_args(argv)

    repo = a.repo.resolve()
    targets = build_map(repo, a.user_root, a.home)
    if a.only:
        targets = [t for t in targets if any(f"{t.tree}/{t.rel}".startswith(p) for p in a.only)]
    if not targets:
        print("deploy: nothing to deploy (no tracked files matched)")
        return 1
    if a.allow_dirty and (a.rev or a.site_rev):
        print("deploy: --allow-dirty and --rev do not combine")
        return 2
    if not a.allow_dirty and a.rev is None:
        d = dirty_files(repo, targets)
        if d:
            print("deploy: refused, uncommitted changes in deployed files (commit, or --allow-dirty):")
            for x in d:
                print(f"  {x}")
            return 2
    tools_root = a.user_root / "codex_tools"
    record_path = tools_root / ".deployed.json"
    record = load_record(record_path)
    # with --allow-dirty the working tree is what gets deployed
    rev = a.rev if a.rev else (None if a.allow_dirty else "HEAD")
    site_rev = a.site_rev if a.site_rev else (None if a.allow_dirty else "HEAD")
    plan = make_plan(repo, targets, record, rev, site_rev)

    print(f"deploy: {len(plan.install)} to install, {len(plan.same)} already current, {len(plan.drift)} drifted")
    for t, data in plan.install:
        print(f"  install {t.tree}/{t.rel} -> {t.live}  sha256={sha256_bytes(data)[:12]}")
    for t, live_sha in plan.drift:
        print(f"  DRIFT  {t.live} was edited live (sha256={live_sha[:12]}), not by a deploy")
    for p in live_only(targets, a.user_root, a.home):
        print(f"  live-only (kept; remove with cdx-agent --rm): {p}")
    if a.dry_run:
        return 0
    if plan.drift and not a.force:
        print("deploy: refused; copy the live edits into the repo first, or pass --force to overwrite them")
        return 2
    if not plan.install:
        return 0
    if not a.skip_tests:
        print("deploy: running tests ...")
        if run_tests(repo) != 0:
            print("deploy: refused, tests failed")
            return 1

    tools_root.mkdir(parents=True, exist_ok=True)
    with open(tools_root / ".deploy.lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        for t, data in plan.install:
            src = tree_root(repo, t.tree) / t.rel
            mode = (src.stat().st_mode & 0o777) if src.exists() else 0o640
            atomic_install(data, t.live, mode)
        files = dict(record.get("files", {}))
        for t, data in plan.install:
            files[str(t.live)] = sha256_bytes(data)
        for t in plan.same:
            files.setdefault(str(t.live), sha256_file(t.live))
        site = repo / "site"
        new_record = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "host": socket.gethostname().split(".")[0],
            "git_sha": _git(repo, "rev-parse", a.rev or "HEAD").strip(),
            "site_sha": _git(site, "rev-parse", a.site_rev or "HEAD").strip() if (site / ".git").exists() else None,
            "dirty": bool(a.allow_dirty),
            "installed": [str(t.live) for t, _ in plan.install],
            "files": files,
        }
        tmp = record_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(new_record, indent=1, sort_keys=True) + "\n")
        os.replace(tmp, record_path)
        with open(tools_root / ".deploy_log.jsonl", "a") as f:
            f.write(json.dumps({k: v for k, v in new_record.items() if k != "files"}) + "\n")
    print(f"deploy: installed {len(plan.install)} file(s); record {record_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
