"""Deletion tiers for the PreToolUse Bash hook (WP3, user policy 2026-11-20).

check(command, cwd) -> None (no delete in the command) or a Verdict:
  allow  every target is tier (a) (allow-listed regenerable junk): no permission decision, logged
  deny   any target tier (b): "use cdx-agent --rm" (moves to trash, restorable for trash.ttl_days)
         any target tier (c): protected, only the user deletes it; the user's own `!` line is printed
         a delete that cannot be parsed (variables, globs that match nothing, xargs, ssh, command
         substitution, python -c rmtree, git clean): deny and point to `cdx-agent --rm`

The classifier is cdx_agent.delete_policy, imported from the CDX-AGENT source tree (env CDX_AGENT_PYTHONPATH,
default /common/home/ss5772/CDX-AGENT/src). If it cannot be imported, every delete is denied (fail closed);
the rest of the hook is unaffected. The hook cannot see deletes inside programs (python scripts, make);
the sandbox is the boundary for those (WP10).

Standalone: stdlib only, no import of pre_tool_use_policy.
"""
from __future__ import annotations

import glob
import json
import os
import re
import shlex
import socket
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

DELETE_HEADS = {"rm", "rmdir", "unlink", "shred"}
WRAPPERS = {"sudo", "command", "exec", "nohup", "nice", "ionice", "builtin", "time"}
OPERATORS = {";", "&&", "||", "|", "&", "\n", "(", ")", ";;", "|&"}
HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n\s*\2\s*(\n|$)", re.S)
DELETE_WORD = re.compile(r"(^|[\s;&|(`$'\"])(rm|rmdir|unlink|shred)\s|-delete\b|git\s+clean\b|worktree\s+remove\b"
                         r"|rmtree|os\.remove|os\.unlink|\.unlink\(|send2trash")
PY_DELETE = re.compile(r"rmtree|os\.remove|os\.unlink|\.unlink\(|os\.rmdir|\.rmdir\(")
RM_HINT = "cdx-agent --rm"


@dataclass
class Verdict:
    action: str                     # "allow" | "deny"
    reason: str
    targets: list[dict] = field(default_factory=list)


def _strip_heredocs(command: str) -> str:
    return HEREDOC.sub(lambda m: m.group(0).split("\n", 1)[0] + "\n", command)


def _tokens(command: str) -> Optional[list[str]]:
    lex = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    lex.commenters = "#"
    try:
        return list(lex)
    except ValueError:
        return None


def _segments(command: str) -> Optional[list[list[str]]]:
    toks = _tokens(_strip_heredocs(command).replace("\\\n", " ").replace("\n", " ; "))
    if toks is None:
        return None
    segs: list[list[str]] = [[]]
    for t in toks:
        if t in OPERATORS or set(t) <= set(";&|()\n"):
            segs.append([])
        else:
            segs[-1].append(t)
    return [s for s in segs if s]


def _head(words: list[str]) -> tuple[str, list[str]]:
    i = 0
    while i < len(words):
        w = words[i]
        base = os.path.basename(w)
        if "=" in w and not w.startswith("-") and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w):
            i += 1
            continue
        if base == "env":
            i += 1
            while i < len(words) and (words[i].startswith("-") or "=" in words[i]):
                i += 1
            continue
        if base == "timeout":
            i += 1
            while i < len(words) and (words[i].startswith("-") or re.fullmatch(r"\d+(\.\d+)?[smhd]?", words[i])):
                i += 1
            continue
        if base in WRAPPERS:
            i += 1
            while i < len(words) and words[i].startswith("-"):
                i += 1
            continue
        return base, words[i + 1:]
    return "", []


def _expand(arg: str, cwd: Path) -> Optional[list[Path]]:
    """Literal path or glob, resolved against cwd. None = cannot be resolved safely."""
    if "$" in arg or "`" in arg or (arg.startswith("~") and arg != "~" and not arg.startswith("~/")):
        return None
    arg = os.path.expanduser(arg)
    p = arg if os.path.isabs(arg) else str(cwd / arg)
    if any(c in arg for c in "*?["):
        hits = glob.glob(p)
        return [Path(h) for h in sorted(hits)] or None
    return [Path(p)]


def _rm_targets(args: list[str]) -> list[str]:
    out, opts_done = [], False
    for a in args:
        if not opts_done and a == "--":
            opts_done = True
            continue
        if not opts_done and a.startswith("-") and a != "-":
            continue
        out.append(a)
    return out


def _find_roots(args: list[str]) -> list[str]:
    roots = []
    for a in args:
        if a.startswith(("-", "(", "!")) or a in {"(", "!"}:
            break
        roots.append(a)
    return roots or ["."]


@dataclass
class Parsed:
    paths: list[str] = field(default_factory=list)     # raw path arguments that would be deleted
    unparsable: list[str] = field(default_factory=list)
    git_worktree: list[str] = field(default_factory=list)


def parse(command: str, depth: int = 0) -> Parsed:
    res = Parsed()
    if not DELETE_WORD.search(command):
        return res
    stripped = _strip_heredocs(command)
    if re.search(r"\$\(|`", stripped) and DELETE_WORD.search(re.sub(r"^[^$`]*", "", stripped)):
        # a delete inside command substitution cannot be resolved
        inner = re.findall(r"\$\(([^)]*)\)|`([^`]*)`", stripped)
        if any(DELETE_WORD.search(a or b) for a, b in inner):
            res.unparsable.append("delete inside command substitution")
    segs = _segments(command)
    if segs is None:
        res.unparsable.append("unbalanced quotes")
        return res
    for words in segs:
        head, args = _head(words)
        if not head:
            continue
        if head in DELETE_HEADS:
            tg = _rm_targets(args)
            if not tg:
                continue
            res.paths.extend(tg)
        elif head == "find":
            if "-delete" in args or any(args[i] in {"-exec", "-execdir", "-ok", "-okdir"} and i + 1 < len(args)
                                        and os.path.basename(args[i + 1]) in DELETE_HEADS for i in range(len(args))):
                res.paths.extend(_find_roots(args))
        elif head == "xargs":
            if any(os.path.basename(a) in DELETE_HEADS for a in args):
                res.unparsable.append("xargs " + " ".join(args[:4]))
        elif head == "git":
            sub = [a for a in args if not a.startswith("-")]
            short = "".join(a[1:] for a in args if a.startswith("-") and not a.startswith("--"))
            if sub[:1] == ["clean"] and ("f" in short or "--force" in args) and not ("n" in short or "--dry-run" in args):
                res.unparsable.append("git clean (deletes untracked files with no undo)")
            elif sub[:2] == ["worktree", "remove"]:
                rest = _rm_targets(args[args.index("remove") + 1:])
                res.git_worktree.extend(rest)
        elif head in {"ssh", "srun", "pdsh"}:
            if DELETE_WORD.search(" ".join(args)):
                res.unparsable.append(f"{head}: remote delete")
        elif head in {"bash", "sh", "zsh"} and "-c" in args:
            i = args.index("-c")
            if i + 1 < len(args) and depth < 2:
                sub = parse(args[i + 1], depth + 1)
                res.paths += sub.paths
                res.unparsable += sub.unparsable
                res.git_worktree += sub.git_worktree
        elif head == "eval" and depth < 2:
            sub = parse(" ".join(args), depth + 1)
            res.paths += sub.paths
            res.unparsable += sub.unparsable
            res.git_worktree += sub.git_worktree
        elif re.fullmatch(r"python(\d+(\.\d+)?)?", head) and "-c" in args:
            i = args.index("-c")
            if i + 1 < len(args) and PY_DELETE.search(args[i + 1]):
                res.unparsable.append("python -c with a delete call")
    return res


def _load_policy_module():
    src = os.environ.get("CDX_AGENT_PYTHONPATH", "/common/home/ss5772/CDX-AGENT/src")
    if src and src not in sys.path:
        sys.path.insert(0, src)
    from cdx_agent import delete_policy, inuse  # noqa: F401  (import errors handled by caller)
    return delete_policy, inuse


def audit(action: str, path: str, **fields) -> None:
    d = Path(os.environ.get("CDX_AUDIT_DIR", "/common/users/ss5772/.cdx/audit"))
    row = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "host": socket.gethostname().split(".")[0],
           "source": "pre_tool_use_hook", "action": action, "path": path, **fields}
    try:
        d.mkdir(parents=True, exist_ok=True)
        with open(d / f"delete_{row['host']}.jsonl", "a") as f:
            f.write(json.dumps(row) + "\n")
    except OSError:
        pass


def _session_scratch(raw_paths: list[str], base: Path, session_id: str) -> Optional[Path]:
    """This session's own scratchpad: Claude Code puts it at .../<session_id>/scratchpad."""
    if not session_id:
        return None
    for raw in raw_paths:
        p = Path(os.path.abspath(os.path.expanduser(raw) if os.path.isabs(os.path.expanduser(raw)) else base / raw))
        for anc in [p, *p.parents]:
            if anc.name == "scratchpad" and anc.parent.name == session_id:
                return anc
    return None


def check(command: str, cwd: Optional[str] = None, session_id: str = "", agent_id: str = "") -> Optional[Verdict]:
    parsed = parse(command)
    if not (parsed.paths or parsed.unparsable or parsed.git_worktree):
        return None
    base = Path(cwd or os.getcwd())
    log = {"session": session_id, "agent": agent_id, "command": command[:500]}
    if parsed.unparsable:
        why = "; ".join(parsed.unparsable)
        audit("denied", "", tier="?", rule_id="unparsable", reason=why, **log)
        return Verdict("deny", f"cdx-agent delete policy: cannot check this delete ({why}). Name the paths and run "
                               f"`{RM_HINT} PATH...` (trash, restorable), or `{RM_HINT} --dry-run PATH...` to see tiers.")
    try:
        delete_policy, inuse = _load_policy_module()
        ctx = delete_policy.default_ctx(base, in_use=lambda p: inuse.in_use(p, local_only=True),
                                        session_scratch=_session_scratch(parsed.paths, base, session_id))
    except Exception as e:  # fail closed for deletes only
        audit("denied", "", tier="?", rule_id="classifier_unavailable", reason=repr(e)[:200], **log)
        return Verdict("deny", f"cdx-agent delete policy: classifier unavailable ({type(e).__name__}); "
                               f"use `{RM_HINT} PATH...`.")
    targets: list[dict] = []
    for raw in parsed.paths + parsed.git_worktree:
        expanded = _expand(raw, base)
        if expanded is None:
            audit("denied", raw, tier="?", rule_id="unresolved", **log)
            return Verdict("deny", f"cdx-agent delete policy: cannot resolve `{raw}` (variable, ~user, or a glob "
                                   f"that matches nothing). Use `{RM_HINT} PATH...` with literal paths.")
        for p in expanded:
            if not os.path.lexists(p):
                continue  # deleting nothing is harmless
            d = delete_policy.classify(Path(os.path.abspath(p)), ctx)
            targets.append({"path": str(p), "tier": d.tier, "rule_id": d.rule_id, "reason": d.reason})
    if not targets:
        return None
    c = [t for t in targets if t["tier"] == "c"]
    b = [t for t in targets if t["tier"] == "b"]
    for t in targets:
        audit("allowed" if not (b or c) else "denied", t["path"], tier=t["tier"], rule_id=t["rule_id"], **log)
    if c:
        lines = ", ".join(f"{t['path']} ({t['rule_id']})" for t in c[:5])
        user_cmd = "! rm -r " + " ".join(shlex.quote(t["path"]) for t in c[:5])
        return Verdict("deny", f"cdx-agent delete policy: protected (tier c): {lines}. Only the user deletes these. "
                               f"Ask the user; their command would be: `{user_cmd}`", targets)
    if b:
        paths = " ".join(shlex.quote(t["path"]) for t in b[:10])
        return Verdict("deny", f"cdx-agent delete policy: not allow-listed (tier b). Run `{RM_HINT} {paths}` "
                               f"(moves to trash, restorable with `cdx-agent --trash restore ID`).", targets)
    return Verdict("allow", "all targets allow-listed (tier a)", targets)
