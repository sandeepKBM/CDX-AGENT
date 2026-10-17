"""Deterministic, no-LLM context brief for seeding a fresh subagent or Codex worker.

    python -m cdx_agent.brief <WORKTREE_DIR | BRANCH> [--base B] [--max-tokens 3000]
        [--repo DIR] [--rules FILE]

Prints a Markdown brief (instead of forking or resuming a long session):
  - branch, HEAD, base and merge-base (base: --base, else the reflog's
    "Created from X", else main/master), ahead/behind, uncommitted summary;
  - commits since the merge-base (oneline, newest first);
  - changed files (numstat, biggest churn first);
  - TODO / Open / Next sections of docs/runbooks/**.md and docs/status/*todo*.md
    changed on the branch (read at the branch tip, not the working tree);
  - headline (title + first table rows) of each reports/**.md changed on the branch;
  - the shared rules block (--rules, $CDX_BRIEF_RULES, else <tools_root>/base/brief_rules.md);
  - an output contract.
Total output is capped at --max-tokens (~4 chars/token); the variable sections
share the budget fairly and are truncated from their tails with a marker.
Same repo state -> byte-identical output (no timestamps, fixed git options).
Exit codes: 0 ok, 2 bad target/base.
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

CHARS_PER_TOKEN = 4
MAX_LINE_CHARS = 200
SECTION_LINE_CAP = 30
TABLE_ROWS = 2
DEFAULT_BASES = ("main", "master")
DOC_GLOBS = ("docs/runbooks/*.md", "docs/status/*todo*.md")
REPORT_GLOB = "reports/*.md"
OPEN_HEADING = re.compile(r"\b(todo|open|next)\b", re.IGNORECASE)
HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")

OUTPUT_CONTRACT = """Reply in <= 15 lines, exactly these keys:
DONE: <path - what changed>, one line each (or "nothing")
TESTS: <command -> result>, one line each (or "not run: why")
COMMITS: <sha subject> or none
UNSURE: every assumption, unverified claim or skipped check ("none" only if truly none)
No logs, diffs or file dumps; point to paths instead."""


class BriefError(Exception):
    pass


def _git(repo: str, *args: str, check: bool = True) -> str:
    env = dict(os.environ, LC_ALL="C", GIT_PAGER="cat")
    proc = subprocess.run(
        ["git", "-c", "core.quotepath=off", "-C", repo, *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        errors="replace", timeout=60,
    )
    if proc.returncode != 0:
        if check:
            raise BriefError(f"git {' '.join(args)}: {proc.stderr.strip() or proc.returncode}")
        return ""
    return proc.stdout.rstrip("\n")


def _commit_exists(repo: str, rev: str) -> bool:
    return bool(_git(repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}", check=False))


def _worktrees(repo: str) -> dict:
    """branch name -> worktree path, from `git worktree list --porcelain`."""
    out, path = {}, None
    for line in _git(repo, "worktree", "list", "--porcelain", check=False).splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line.startswith("branch refs/heads/") and path:
            out[line[len("branch refs/heads/"):]] = path
    return out


def resolve_target(target: str, repo: Optional[str]) -> Tuple[str, str, str, Optional[str]]:
    """-> (repo_toplevel, branch_label, tip_rev, worktree_path_or_None)."""
    if os.path.isdir(target):
        top = _git(target, "rev-parse", "--show-toplevel", check=False)
        if not top:
            raise BriefError(f"{target}: not inside a git work tree")
        branch = _git(top, "symbolic-ref", "--short", "-q", "HEAD", check=False)
        return top, branch or "(detached HEAD)", "HEAD", top
    top = _git(repo or ".", "rev-parse", "--show-toplevel", check=False)
    if not top:
        raise BriefError(f"{repo or os.getcwd()}: not inside a git work tree (use --repo DIR)")
    if not _commit_exists(top, target):
        raise BriefError(f"{target}: neither a directory nor a revision in {top}")
    return top, target, target, _worktrees(top).get(target)


def resolve_base(repo: str, branch: str, tip: str, base: Optional[str]) -> Tuple[str, str]:
    """-> (base_rev, how_it_was_chosen)."""
    if base:
        if not _commit_exists(repo, base):
            raise BriefError(f"--base {base}: unknown revision")
        return base, "--base"
    if not branch.startswith("("):
        reflog = _git(repo, "reflog", "show", "--format=%gs", f"refs/heads/{branch}", "--", check=False)
        for line in reversed(reflog.splitlines()):  # oldest entry first
            match = re.match(r"branch: Created from (\S+)$", line.strip())
            if match:
                origin = match.group(1)
                if origin != "HEAD" and origin != branch and _commit_exists(repo, origin):
                    return origin, "reflog: created from"
                break
    for name in DEFAULT_BASES:
        if name != branch and _commit_exists(repo, name):
            return name, "default"
    raise BriefError("no base found: pass --base B")


def _clip(line: str) -> str:
    return line if len(line) <= MAX_LINE_CHARS else line[: MAX_LINE_CHARS - 3] + "..."


def _status_summary(repo: str) -> str:
    counts: Counter = Counter()
    for line in _git(repo, "status", "--porcelain", check=False).splitlines():
        xy = line[:2]
        if xy == "??":
            counts["untracked"] += 1
        elif "U" in xy or xy in ("AA", "DD"):
            counts["conflicted"] += 1
        elif "D" in xy:
            counts["deleted"] += 1
        elif xy[0] == "A":
            counts["added"] += 1
        else:
            counts["modified"] += 1
    if not counts:
        return "clean"
    order = ("modified", "added", "deleted", "conflicted", "untracked")
    return ", ".join(f"{counts[k]} {k}" for k in order if counts[k])


def changed_paths(repo: str, mb: str, tip: str) -> List[str]:
    out = _git(repo, "diff", "--name-only", "--diff-filter=d", "--no-renames", mb, tip)
    return sorted(p for p in out.splitlines() if p)


def _matches(path: str, pattern: str) -> bool:
    return fnmatch.fnmatch(path.lower(), pattern.lower())


def extract_open_sections(text: str, whole_if_none: bool = False) -> List[Tuple[str, List[str]]]:
    """Heading sections whose title says TODO/Open/Next -> [(heading, body_lines)].
    Body runs to the next heading of the same or higher level; blank lines and
    headings inside code fences are ignored."""
    lines = text.splitlines()
    sections: List[Tuple[str, List[str]]] = []
    current: Optional[Tuple[int, str, List[str]]] = None
    in_fence = False
    for line in lines:
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
        heading = None if in_fence else HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            if current and level <= current[0]:
                sections.append((current[1], current[2]))
                current = None
            if current is None and OPEN_HEADING.search(heading.group(2)):
                current = (level, line.strip(), [])
                continue
        if current is not None and line.strip():
            current[2].append(_clip(line.rstrip()))
    if current:
        sections.append((current[1], current[2]))
    if not sections and whole_if_none:
        body = [_clip(l.rstrip()) for l in lines if l.strip()]
        if body:
            sections.append(("(whole file)", body))
    return sections


def report_headline(text: str) -> List[str]:
    """Title (first heading, else first non-empty line) + first table's header and rows."""
    title, table, in_table = "", [], False
    for line in text.splitlines():
        stripped = line.strip()
        if not title and stripped:
            match = HEADING.match(stripped)
            title = match.group(2) if match else stripped
            continue
        if stripped.startswith("|"):
            in_table = True
            if re.fullmatch(r"\|?[\s:|-]+\|?", stripped):
                continue  # separator row
            table.append("  " + _clip(stripped))
            if len(table) > TABLE_ROWS:
                break
        elif in_table:
            break
    return [_clip(title or "(untitled)")] + table


def _section(title: str, body: List[str], empty: str = "(none)") -> Tuple[str, List[str]]:
    return title, body if body else [empty]


def _fit(body: List[str], budget: int, hint: str) -> List[str]:
    """Keep whole lines from the head within `budget` chars (incl. newlines)."""
    total = sum(len(l) + 1 for l in body)
    if total <= budget:
        return body
    kept: List[str] = []
    used = 0
    for i, line in enumerate(body):
        marker = f"... (+{len(body) - i} more lines{hint})"
        if used + len(line) + 1 + len(marker) + 1 > budget:
            return kept + [marker] if used + len(marker) + 1 <= budget or not kept else kept
        kept.append(line)
        used += len(line) + 1
    return kept


def _share(sizes: List[int], budget: int) -> List[int]:
    """Water-filling: sections smaller than the fair share keep their size; the
    leftover is split evenly over the rest."""
    alloc = [0] * len(sizes)
    pending = list(range(len(sizes)))
    while pending:
        share = max(budget, 0) // len(pending)
        small = [i for i in pending if sizes[i] <= share]
        if not small:
            for i in pending:
                alloc[i] = share
            break
        for i in small:
            alloc[i] = sizes[i]
            budget -= sizes[i]
            pending.remove(i)
    return alloc


def _render(title: str, body: List[str]) -> str:
    return "\n".join([title, *body]) + "\n"


def default_rules_path() -> Path:
    env = os.environ.get("CDX_BRIEF_RULES")
    if env:
        return Path(env)
    try:
        from cdx_agent.config import load_config
        return load_config().tools_root / "base" / "brief_rules.md"
    except Exception:
        return Path.home() / "codex_tools" / "base" / "brief_rules.md"


def build_brief(target: str, base: Optional[str] = None, max_tokens: int = 3000,
                repo: Optional[str] = None, rules_path: Optional[str] = None) -> str:
    top, branch, tip, worktree = resolve_target(target, repo)
    base_rev, how = resolve_base(top, branch, tip, base)
    mb = _git(top, "merge-base", base_rev, tip, check=False)
    if not mb:
        raise BriefError(f"no merge-base between {base_rev} and {branch}")
    head = _git(top, "log", "-1", "--no-decorate", "--format=%h %s", tip)
    counts = _git(top, "rev-list", "--left-right", "--count", f"{base_rev}...{tip}").split()
    behind, ahead = (counts + ["?", "?"])[:2]

    header = [f"# Brief: {branch}"] + ([] if worktree == top else [f"- repo: {top}"])
    if worktree:
        header.append(f"- worktree: {worktree} (uncommitted: {_status_summary(worktree)})")
    header += [
        f"- head: {_clip(head)}",
        f"- base: {base_rev} ({how}); merge-base {mb[:10]}; ahead {ahead}, behind {behind}",
    ]

    commits = _git(top, "log", "--no-decorate", "--no-merges", "--format=%h %s", f"{mb}..{tip}")
    commit_lines = [_clip(l) for l in commits.splitlines() if l]

    numstat = _git(top, "diff", "--numstat", "--no-renames", mb, tip)
    rows, tot_ins, tot_del = [], 0, 0
    for line in numstat.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        ins, dels, path = parts
        n_ins, n_del = (int(ins) if ins.isdigit() else 0), (int(dels) if dels.isdigit() else 0)
        tot_ins, tot_del = tot_ins + n_ins, tot_del + n_del
        label = "bin" if ins == "-" else f"+{ins} -{dels}"
        rows.append((-(n_ins + n_del), path, _clip(f"{label} {path}")))
    rows.sort()
    file_lines = [r[2] for r in rows]

    paths = changed_paths(top, mb, tip)
    doc_lines: List[str] = []
    for path in paths:
        if not any(_matches(path, g) for g in DOC_GLOBS):
            continue
        text = _git(top, "show", f"{tip}:{path}", check=False)
        is_todo_file = _matches(path, "docs/status/*todo*.md")
        for heading, body in extract_open_sections(text, whole_if_none=is_todo_file):
            doc_lines.append(_clip(f"{path} > {heading}"))
            doc_lines.extend(body[:SECTION_LINE_CAP])
            if len(body) > SECTION_LINE_CAP:
                doc_lines.append(f"... (+{len(body) - SECTION_LINE_CAP} more lines in {path})")

    report_lines: List[str] = []
    for path in paths:
        if not _matches(path, REPORT_GLOB):
            continue
        headline = report_headline(_git(top, "show", f"{tip}:{path}", check=False))
        report_lines.append(_clip(f"- {path}: {headline[0]}"))
        report_lines.extend(headline[1:])

    rules_file = Path(rules_path) if rules_path else default_rules_path()
    try:
        rules = [l.rstrip() for l in rules_file.read_text().splitlines() if l.strip()]
        rules = [l for l in rules if not l.startswith("# ")]  # drop the file's own title
    except OSError:
        rules = [f"(rules file missing: {rules_file})"]

    rng = f"{mb[:10]}..{tip}"
    variable = [
        (_section(f"## Commits since base ({len(commit_lines)})", commit_lines), f"; git log --oneline {rng}"),
        (_section(f"## Changed files ({len(file_lines)}, +{tot_ins} -{tot_del})", file_lines),
         f"; git diff --stat {rng}"),
        (_section("## Open items (TODO/Open/Next in docs changed on branch)", doc_lines), ""),
        (_section("## Report headlines (reports/**.md changed on branch)", report_lines), ""),
    ]
    fixed_head = "\n".join(header) + "\n"
    contract = _render("## Output contract", OUTPUT_CONTRACT.splitlines())
    budget = max_tokens * CHARS_PER_TOKEN
    # Empty sections ("(none)") are tiny and always shown verbatim.
    titles = sum(len(_render(title, body)) if body == ["(none)"] else len(title) + 1
                 for (title, body), _ in variable)
    # Room for each section's worst-case truncation marker, so the hard cut below never fires
    # unless the fixed parts alone are over the cap.
    slack = 8 + sum(len(f"... (+{len(body)} more lines{hint})") + 1
                    for (_, body), hint in variable if body != ["(none)"])
    rules_room = budget - len(fixed_head) - len(contract) - titles - slack - len("## Rules\n")
    rules_text = _render("## Rules", _fit(rules, max(rules_room, 0), "; see the rules file") or ["..."])
    room = budget - len(fixed_head) - len(rules_text) - len(contract) - titles - slack
    sizes = [0 if body == ["(none)"] else sum(len(l) + 1 for l in body) for (_, body), _ in variable]
    alloc = _share(sizes, max(room, 0))
    parts = [fixed_head]
    for ((title, body), hint), cap in zip(variable, alloc):
        shown = body if body == ["(none)"] else (_fit(body, cap, hint) or ["..."])
        parts.append(_render(title, shown))
    parts += [rules_text, contract]
    text = "".join(parts)
    if len(text) > budget:  # fixed parts alone exceed the cap: hard cut, keep it honest
        cut = f"\n... (brief cut at --max-tokens {max_tokens})\n"
        text = text[: max(budget - len(cut), 0)].rsplit("\n", 1)[0] + cut
    return text


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cdx-agent --brief", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("target", help="worktree directory or branch name")
    p.add_argument("--base", default=None, help="base revision (default: reflog 'Created from', else main/master)")
    p.add_argument("--max-tokens", type=int, default=3000, help="output cap, ~4 chars/token (default 3000)")
    p.add_argument("--repo", default=None, help="repo to resolve a branch name in (default: cwd)")
    p.add_argument("--rules", default=None, help="rules block file (default: $CDX_BRIEF_RULES or "
                                                 "<tools_root>/base/brief_rules.md)")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.max_tokens < 200:
        print("brief: --max-tokens must be >= 200", file=sys.stderr)
        return 2
    try:
        sys.stdout.write(build_brief(args.target, args.base, args.max_tokens, args.repo, args.rules))
    except BriefError as exc:
        print(f"brief: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
