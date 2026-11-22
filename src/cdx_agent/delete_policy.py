"""Deletion tier classifier: (a) agent may delete, (b) trash with manifest, (c) user-only.

Precedence: (1) hard rules -> c; (2) allow list -> a; (3) protect patterns -> c (critical=True; with
a verified second copy -> b but still critical); (4) otherwise b.
`critical` means: never auto-purged from trash.
"""
from __future__ import annotations

import copy
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import hygiene

WALK_LIMIT = 20000
DEFAULT_BASE = Path("/common/users/ss5772/codex_tools/base/delete_policy.json")
PACKAGE_TEMPLATE = Path(__file__).resolve().parents[2] / "tools" / "templates" / "delete_policy.json"
DEFAULT_TRASH_ROOTS = [
    "/data/local/ss5772/trash",
    "/freespace/local/ss5772/trash",
    "/common/users/ss5772/.cdx/trash_staging",
    "/common/home/ss5772/.cdx/trash_staging",
]


@dataclass
class Decision:
    tier: str  # "a" | "b" | "c"
    rule_id: str
    reason: str
    critical: bool = False


@dataclass
class Ctx:
    session_scratch: Optional[Path]
    home: Path
    project_roots: List[Path]
    trash_roots: List[Path]
    uid: int
    in_use: Optional[Callable[[Path], dict]] = None
    verified_copy: bool = False
    policy: dict = field(default_factory=dict)


# --- policy loading -----------------------------------------------------------------


def _git_out(cwd: Path, *args: str) -> Optional[str]:
    try:
        r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def main_checkout(repo: Path) -> Optional[Path]:
    """Main checkout of the repo containing `repo` (worktrees resolve to the main one)."""
    out = _git_out(repo, "rev-parse", "--git-common-dir")
    if not out:
        return None
    common = (repo / out).resolve()
    return common.parent if common.name == ".git" else common


def _read_json(p: Path) -> Optional[dict]:
    try:
        d = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def _as_list(v: Any) -> List[str]:
    if v is None:
        return []
    return [v] if isinstance(v, str) else [str(x) for x in v]


def _expand(s: str, home: Path, scratch: Optional[Path]) -> Optional[str]:
    s = s.replace("${HOME}", str(home))
    if "${SESSION_SCRATCH}" in s:
        if scratch is None:
            return None
        s = s.replace("${SESSION_SCRATCH}", str(scratch))
    return s


def _pre_expand(entry: dict) -> dict:
    """Expand ${HOME} and (if the env provides it) ${SESSION_SCRATCH} in prefix/glob values."""
    home = Path.home()
    scratch = os.environ.get("CDX_SESSION_SCRATCH")
    e = dict(entry)
    for key in ("prefix", "glob"):
        if key in e:
            vals = []
            for v in _as_list(e[key]):
                x = v.replace("${HOME}", str(home))
                if scratch:
                    x = x.replace("${SESSION_SCRATCH}", scratch)
                vals.append(x)
            e[key] = vals
    return e


def load_policy(base: Optional[Path] = None, repo: Optional[Path] = None) -> dict:
    warnings: List[str] = []
    candidates = [Path(base)] if base else []
    if not base:
        if os.environ.get("CDX_DELETE_POLICY"):
            candidates.append(Path(os.environ["CDX_DELETE_POLICY"]))
        candidates += [DEFAULT_BASE, PACKAGE_TEMPLATE]
    pol: dict = {"version": 1, "allow": [], "protect": []}
    for c in candidates:
        d = _read_json(c)
        if d is not None:
            pol = copy.deepcopy(d)
            pol.setdefault("allow", [])
            pol.setdefault("protect", [])
            break
    pol["allow"] = [_pre_expand(e) for e in pol["allow"]]
    pol["protect"] = [_pre_expand(e) for e in pol["protect"]]
    if repo is not None:
        main = main_checkout(Path(repo)) or Path(repo).resolve()
        proj = _read_json(main / ".cdx" / "delete_policy.json") or {}
        for e in proj.get("protect", []):
            if isinstance(e, dict):
                pol["protect"].append(_pre_expand(e))
        root = str(main)
        for e in proj.get("allow", []):
            if not isinstance(e, dict):
                continue
            e = _pre_expand(e)
            ok = [v for k in ("prefix", "glob") for v in _as_list(e.get(k)) if _under_str(v, root)]
            kinds = [k for k in e if k not in ("id",)]
            if not ok or any(k not in ("prefix", "glob") for k in kinds):
                warnings.append(f"project allow entry {e.get('id', '?')!r} ignored: allow is only valid for prefix/glob under {root}")
                continue
            keep = {"id": e.get("id", "project")}
            for k in ("prefix", "glob"):
                if k in e:
                    keep[k] = [v for v in _as_list(e[k]) if _under_str(v, root)]
            pol["allow"].append(keep)
    pol["warnings"] = warnings
    return pol


def _under_str(p: str, root: str) -> bool:
    if not p.startswith("/") or ".." in p.split("/"):
        return False
    return p == root or p.startswith(root.rstrip("/") + "/")


# --- context ------------------------------------------------------------------------


def default_ctx(repo: Optional[Path] = None, verified_copy: bool = False, *,
                in_use: Optional[Callable[[Path], dict]] = None,
                session_scratch: Optional[Path] = None) -> Ctx:
    cwd = Path(repo) if repo else Path.cwd()
    roots: List[Path] = []
    top = _git_out(cwd, "rev-parse", "--show-toplevel")
    if top:
        roots.append(Path(top).resolve())
        main = main_checkout(cwd)
        if main and main not in roots:
            roots.append(main)
    if session_scratch is None and os.environ.get("CDX_SESSION_SCRATCH"):
        session_scratch = Path(os.environ["CDX_SESSION_SCRATCH"])
    try:
        trash = json.loads(os.environ["CDX_TRASH_ROOTS"]) if "CDX_TRASH_ROOTS" in os.environ else DEFAULT_TRASH_ROOTS
        if not isinstance(trash, list):
            trash = DEFAULT_TRASH_ROOTS
    except ValueError:
        trash = DEFAULT_TRASH_ROOTS
    if in_use is None:
        from . import inuse

        in_use = inuse.in_use
    return Ctx(
        session_scratch=session_scratch,
        home=Path.home(),
        project_roots=roots,
        trash_roots=[Path(t) for t in trash],
        uid=os.getuid(),
        in_use=in_use,
        verified_copy=verified_copy,
        policy=load_policy(None, repo if repo else (Path(top) if top else None)),
    )


# --- matching -----------------------------------------------------------------------


def glob_to_regex(pat: str) -> "re.Pattern[str]":
    out, i = "", 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pat.startswith("/**", i) and i + 3 == len(pat):
            out += "(?:/.*)?"
            i += 3
        elif pat.startswith("**", i):
            out += ".*"
            i += 2
        elif pat[i] == "*":
            out += "[^/]*"
            i += 1
        elif pat[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pat[i])
            i += 1
    return re.compile("^" + out + "$")


def _is_under(p: str, prefix: str) -> bool:
    prefix = prefix.rstrip("/") or "/"
    return p == prefix or p.startswith(prefix.rstrip("/") + "/")


def _protect_id(p: str, protect: List[dict], ctx: Ctx) -> Optional[str]:
    for e in protect:
        for g in _as_list(e.get("glob")):
            x = _expand(g, ctx.home, ctx.session_scratch)
            if x and glob_to_regex(x).match(p):
                return str(e.get("id", "protect"))
        for pre in _as_list(e.get("prefix")):
            x = _expand(pre, ctx.home, ctx.session_scratch)
            if x and _is_under(p, x):
                return str(e.get("id", "protect"))
    return None


def _contains_protected(root: str, protect: List[dict], ctx: Ctx) -> Optional[str]:
    # A protected prefix located below `root` makes root itself protected.
    for e in protect:
        for pre in _as_list(e.get("prefix")):
            x = _expand(pre, ctx.home, ctx.session_scratch)
            if x and _is_under(x, root):
                return str(e.get("id", "protect"))
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for n in dirnames + filenames:
            seen += 1
            if seen > WALK_LIMIT:
                return "too_large"
            pid = _protect_id(os.path.join(dirpath, n), protect, ctx)
            if pid:
                return pid
    return None


def _is_secondary_worktree(path: Path) -> bool:
    gd = _git_out(path, "rev-parse", "--absolute-git-dir")
    cd = _git_out(path, "rev-parse", "--git-common-dir")
    top = _git_out(path, "rev-parse", "--show-toplevel")
    if not gd or not cd or not top:
        return False
    if Path(top).resolve() != path.resolve():
        return False
    return Path(gd).resolve() != (path / cd).resolve()


def _worktree_mergeable(path: Path) -> bool:
    if not _is_secondary_worktree(path):
        return False
    status = hygiene._git(path, "status", "--porcelain")
    if status is None or status.strip():
        return False
    head = (hygiene._git(path, "rev-parse", "HEAD") or "").strip()
    branch = (hygiene._git(path, "rev-parse", "--abbrev-ref", "HEAD") or "").strip()
    if branch == "HEAD":
        branch = ""
    main = main_checkout(path)
    return bool(main and hygiene.contained_in_other_branch(main, head, branch))


# --- classifier ---------------------------------------------------------------------


def classify(path: Any, ctx: Ctx) -> Decision:
    raw = Path(path)
    p = os.path.join(os.path.realpath(raw.parent), raw.name) if raw.name else str(raw)
    pol = ctx.policy or {}

    # (1) hard rules
    if ctx.in_use is not None:
        v = ctx.in_use(Path(p))
        if v.get("verdict", "unknown") != "free":
            return Decision("c", "in_use", f"in use ({v.get('verdict')}): {'; '.join(v.get('evidence', [])[:3])}", False)
    try:
        if os.lstat(p).st_uid != ctx.uid:
            return Decision("c", "foreign_owner", "owned by another user", False)
    except OSError:
        pass
    if ".git" in Path(p).parts:
        return Decision("c", "git_dir", "is or is inside a .git directory", False)
    for t in ctx.trash_roots:
        if _is_under(p, str(t)):
            return Decision("c", "trash_root", "inside a trash root", False)
    if os.path.ismount(p):
        return Decision("c", "mount_point", "mount point", False)
    home = str(ctx.home)
    if _is_under(home, p):
        return Decision("c", "home", "is $HOME or an ancestor of it", False)
    for r in ctx.project_roots:
        if _is_under(str(r), p):
            return Decision("c", "project_root", "is a project root or an ancestor of one", False)

    # (2) allow list
    for e in pol.get("allow", []):
        rid = str(e.get("id", "allow"))
        if Path(p).name in _as_list(e.get("names")):
            return Decision("a", rid, f"allow-listed name {Path(p).name}", False)
        for pre in _as_list(e.get("prefix")):
            x = _expand(pre, ctx.home, ctx.session_scratch)
            if x and _is_under(p, x):
                return Decision("a", rid, f"under allow-listed prefix {x}", False)
        for g in _as_list(e.get("glob")):
            x = _expand(g, ctx.home, ctx.session_scratch)
            if x and glob_to_regex(x).match(p):
                return Decision("a", rid, f"matches allow glob {x}", False)
        if "git_worktree" in e and os.path.isdir(p) and _worktree_mergeable(Path(p)):
            return Decision("a", rid, "clean git worktree whose HEAD is contained in another branch", False)
        # "manifest_scratch": no match until job manifests exist (placeholder).

    # (3) protect patterns
    protect = pol.get("protect", [])
    pid = _protect_id(p, protect, ctx)
    why = "matches protect pattern"
    if pid is None and os.path.isdir(p) and not os.path.islink(p):
        pid = _contains_protected(p, protect, ctx)
        why = "contains protected files"
    if pid is not None:
        if ctx.verified_copy:
            return Decision("b", pid, f"{why}; verified second copy exists", True)
        return Decision("c", pid, why, True)

    # (4) default
    return Decision("b", "default", "not allow-listed or protected", False)
