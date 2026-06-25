from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
import re

from .graph import resolve_repo_root


__all__ = [
    "command_context_budget",
    "command_summarize_output",
    "command_summarize_log",
    "command_safe_find",
    "command_safe_rg",
    "command_safe_git_diff",
]


CONTEXT_RISKY_DIRS = {
    "logs",
    "outputs",
    "checkpoints",
    "ckpts",
    "wandb",
    "runs",
    "data",
    "datasets",
    "videos",
    "media",
    "__pycache__",
    ".git",
}

CONTEXT_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__"}

ERROR_RE = re.compile(r"(traceback|error|exception|failed|assert|oom|nan|inf|segmentation fault|fatal)", re.IGNORECASE)
PATH_RE = re.compile(r"([A-Za-z0-9_./-]+\.[A-Za-z0-9_]+(?::\d+)?)")
NOISE_RE = re.compile(
    r"(\r|^\.+$|^\s*\d+%|\|\s*\d+/\d+|\bETA\b|downloading|extracting|installing|fetching|^\s*\[[=> -]+\]\s*$)",
    re.IGNORECASE,
)
KEY_RE = re.compile(
    r"(traceback|error|warning|exception|cuda|oom|nan|inf|loss|checkpoint|dataset|config|success|failure|timeout|reward|eval|episode|crash|reset)",
    re.IGNORECASE,
)
DIFF_IGNORE_GLOBS = (
    "!**/.git/**",
    "!**/logs/**",
    "!**/wandb/**",
    "!**/outputs/**",
    "!**/checkpoints/**",
    "!**/ckpts/**",
    "!**/datasets/**",
    "!**/data/**",
    "!**/videos/**",
    "!**/__pycache__/**",
)


def _read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8", errors="ignore").splitlines()


def _compress_runs(lines: list[str]) -> list[str]:
    if not lines:
        return []
    out: list[str] = []
    current = lines[0]
    count = 1
    for line in lines[1:]:
        if line == current:
            count += 1
            continue
        if count >= 4 and (NOISE_RE.search(current) or len(current) < 120):
            out.append(f"[repeated {count}x] {current}")
        else:
            out.extend([current] * count)
        current = line
        count = 1
    if count >= 4 and (NOISE_RE.search(current) or len(current) < 120):
        out.append(f"[repeated {count}x] {current}")
    else:
        out.extend([current] * count)
    return out


def _interesting_lines(lines: list[str], limit: int = 60) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for idx, line in enumerate(lines):
        if ERROR_RE.search(line) or PATH_RE.search(line) or line.startswith(("E ", "F ", "FAIL", "ERROR", "WARN", "WARNING", "Traceback")):
            snippet = f"L{idx + 1}: {line}"
            if snippet not in seen:
                out.append(snippet)
                seen.add(snippet)
        if len(out) >= limit:
            break
    return out


def _traceback_blocks(lines: list[str], limit: int = 3) -> list[list[str]]:
    blocks: list[list[str]] = []
    idx = 0
    while idx < len(lines):
        if lines[idx].startswith("Traceback"):
            block = [lines[idx]]
            idx += 1
            while idx < len(lines) and lines[idx].strip():
                block.append(lines[idx])
                idx += 1
            blocks.append(block[:20])
            if len(blocks) >= limit:
                break
        idx += 1
    return blocks


def _changed_file_lines(lines: list[str], limit: int = 40) -> list[str]:
    matched: list[str] = []
    for line in lines:
        if line.startswith(("diff --git", "+++ ", "--- ", "@@ ", " M ", " A ", " D ")) or "|" in line:
            matched.append(line)
        if len(matched) >= limit:
            break
    return matched


def _unique_paths(lines: list[str], limit: int = 20) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for line in lines:
        for match in PATH_RE.findall(line):
            if match not in seen:
                seen.add(match)
                found.append(match)
                if len(found) >= limit:
                    return found
    return found


def _format_bytes(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            return f"{value:.1f}{unit}"
        value /= 1024.0
    return f"{size}B"


def _walk_repo(repo: Path, max_files: int) -> tuple[list[tuple[int, str]], dict[str, int], list[str], bool]:
    files: list[tuple[int, str]] = []
    top_sizes: dict[str, int] = defaultdict(int)
    risky_dirs: list[str] = []
    scanned = 0
    truncated = False
    for root, dirs, filenames in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in {".codex_graph"}]
        root_path = Path(root)
        for dirname in list(dirs):
            if dirname in CONTEXT_RISKY_DIRS:
                risky_dirs.append(str((root_path / dirname).relative_to(repo)))
        dirs[:] = [d for d in dirs if d not in CONTEXT_SKIP_DIRS]
        for filename in filenames:
            path = root_path / filename
            if ".git/" in str(path):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            rel = str(path.relative_to(repo))
            files.append((size, rel))
            top = rel.split("/", 1)[0]
            top_sizes[top] += size
            scanned += 1
            if scanned >= max_files:
                truncated = True
                return files, top_sizes, sorted(set(risky_dirs)), truncated
    return files, top_sizes, sorted(set(risky_dirs)), truncated


def command_context_budget(args: argparse.Namespace) -> int:
    repo = resolve_repo_root(args.repo, force_home_scan=bool(getattr(args, "force_home_scan", False)))
    files, top_sizes, risky_dirs, truncated = _walk_repo(repo, args.max_files)
    files_sorted = sorted(files, key=lambda item: item[0], reverse=True)
    dirs_sorted = sorted(top_sizes.items(), key=lambda item: item[1], reverse=True)
    print(f"Repo: {repo}")
    print(f"Files scanned: {len(files)}")
    print(f"Scan truncated: {'yes' if truncated else 'no'}")
    print()
    print("Largest files:")
    for size, rel in files_sorted[:20]:
        print(f"- {_format_bytes(size):>8} {rel}")
    print()
    print("Largest top-level directories/files:")
    for rel, size in dirs_sorted[:20]:
        print(f"- {_format_bytes(size):>8} {rel}")
    print()
    print("Likely generated or token-risk directories:")
    if risky_dirs:
        for rel in risky_dirs[:50]:
            print(f"- {rel}")
    else:
        print("- none detected")
    print()
    print("Suggested ignore patterns:")
    for pattern in (".git/", "logs/", "outputs/", "checkpoints/", "ckpts/", "wandb/", "runs/", "data/", "datasets/", "videos/", "media/", "__pycache__/"):
        print(f"- {pattern}")
    print()
    print("Recommendation: run cdx-agent --graph before broad exploration.")
    return 0


def command_summarize_output(args: argparse.Namespace) -> int:
    path = Path(args.input)
    lines = _read_lines(path)
    compact = _compress_runs(lines)
    print(f"Command: {args.command or 'unknown'}")
    print(f"Exit code: {args.exit_code}")
    print(f"Source: {path}")
    print(f"Bytes: {path.stat().st_size if path.exists() else 0}")
    print(f"Line count: {len(lines)}")
    print()

    paths = _unique_paths(lines)
    if paths:
        print("Path hints:")
        for item in paths:
            print(f"- {item}")
        print()

    tracebacks = _traceback_blocks(lines)
    if tracebacks:
        print("Traceback blocks:")
        for block in tracebacks:
            print("```text")
            for line in block:
                print(line)
            print("```")
        print()

    highlights = _interesting_lines(compact)
    if highlights:
        print("Highlights:")
        for line in highlights:
            print(f"- {line}")
        print()

    changed = _changed_file_lines(compact)
    if changed:
        print("Changed-file or diff hints:")
        for line in changed[:40]:
            print(f"- {line}")
        print()

    print(f"Head ({min(args.head_lines, len(compact))} lines):")
    print("```text")
    for line in compact[: args.head_lines]:
        print(line)
    print("```")
    print()
    print(f"Tail ({min(args.tail_lines, len(compact))} lines):")
    print("```text")
    for line in compact[-args.tail_lines :]:
        print(line)
    print("```")
    return 0


def command_summarize_log(args: argparse.Namespace) -> int:
    path = Path(args.input)
    lines = _read_lines(path)
    print(f"Log: {path}")
    print(f"Bytes: {path.stat().st_size if path.exists() else 0}")
    print(f"Line count: {len(lines)}")
    print()

    blocks = _traceback_blocks(lines)
    if blocks:
        print("Traceback blocks:")
        for block in blocks:
            print("```text")
            for line in block:
                print(line)
            print("```")
        print()

    important = [f"L{idx + 1}: {line}" for idx, line in enumerate(lines) if KEY_RE.search(line)][:120]
    if important:
        print("Important lines:")
        for line in important:
            print(f"- {line}")
        print()

    print(f"Head ({min(args.head_lines, len(lines))} lines):")
    print("```text")
    for line in lines[: args.head_lines]:
        print(line)
    print("```")
    print()
    print(f"Tail ({min(args.tail_lines, len(lines))} lines):")
    print("```text")
    for line in lines[-args.tail_lines :]:
        print(line)
    print("```")
    return 0


def command_safe_find(args: argparse.Namespace) -> int:
    grouped: dict[str, list[str]] = defaultdict(list)
    counter: Counter[str] = Counter()
    total = 0
    for base in args.paths or ["."]:
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in CONTEXT_SKIP_DIRS and d != ".codex_graph"]
            for filename in files:
                path = str(Path(root) / filename)
                ext = Path(filename).suffix or "<no-ext>"
                counter[ext] += 1
                if total < args.max_results:
                    grouped[ext].append(path)
                total += 1
    print(f"Files matched: {total}")
    print("Counts by extension:")
    for ext, count in counter.most_common(20):
        print(f"- {ext}: {count}")
    print()
    print("Sample paths:")
    shown = 0
    for ext, paths in sorted(grouped.items()):
        for path in paths[:10]:
            print(f"- [{ext}] {path}")
            shown += 1
            if shown >= args.max_results:
                return 0
    if total > args.max_results:
        print(f"[safe_find] truncated output after {args.max_results} sample paths.")
    return 0


def command_safe_rg(args: argparse.Namespace) -> int:
    rg = shutil.which("rg")
    if rg is None:
        print("rg is not installed", file=sys.stderr)
        return 127
    cmd = [rg, "--line-number", "--color", "never"]
    for glob in (
        "!**/.git/**",
        "!**/logs/**",
        "!**/wandb/**",
        "!**/outputs/**",
        "!**/checkpoints/**",
        "!**/ckpts/**",
        "!**/datasets/**",
        "!**/data/**",
        "!**/videos/**",
        "!**/__pycache__/**",
    ):
        cmd.extend(["--glob", glob])
    cmd.append(args.pattern)
    cmd.extend(args.paths or ["."])
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
    lines = result.stdout.splitlines()
    for line in lines[: args.max_lines]:
        print(line)
    if len(lines) > args.max_lines:
        print(f"[safe_rg] truncated {len(lines) - args.max_lines} additional lines; narrow the query or paths.", file=sys.stderr)
    return result.returncode


def command_safe_git_diff(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()

    def run(*git_args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(repo), *git_args],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        return result.stdout

    print("git diff --stat")
    print(run("diff", "--stat").rstrip())
    print()
    print("changed files")
    print(run("status", "--short").rstrip())
    if args.show_hunks and args.files:
        for file_name in args.files:
            print()
            print(f"hunks for {file_name}")
            print(run("diff", "--", file_name).rstrip())
    elif args.show_hunks:
        print()
        print("No files specified for --show-hunks; use --file path/to/file")
    return 0
