"""Repo hygiene checks: ``python -m cdx_agent.check [repo|docs|report FILE|refs PHRASE] [--repo DIR]``.

Exit 1 when there are findings, 0 when clean.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

from .project import cdx_dir, load_project, load_routes, long_suites
from .suite import SKIP_DIRS, glob_match


def _files(root: Path) -> list[str]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            out.append((Path(dirpath) / name).relative_to(root).as_posix())
    return out


def check_repo(root: Path) -> list[str]:
    findings: list[str] = []
    cdx = cdx_dir(root)
    if not cdx.is_dir():
        return [".cdx/: missing"]
    project = cdx / "project.json"
    if project.is_file():
        try:
            load_project(root)
        except (ValueError, json.JSONDecodeError) as exc:
            findings.append(f".cdx/project.json: does not parse: {exc}")
    resources = cdx / "resources.yaml"
    if resources.is_file():
        try:
            yaml.safe_load(resources.read_text())
        except yaml.YAMLError as exc:
            findings.append(f".cdx/resources.yaml: does not parse: {str(exc).splitlines()[0]}")
    files = _files(root)
    try:
        routes = load_routes(root)
    except (ValueError, yaml.YAMLError) as exc:
        findings.append(f".cdx/test_routes.yaml: does not parse: {str(exc).splitlines()[0]}")
        routes = {"routes": []}
    for route in routes["routes"]:
        if not any(glob_match(f, str(route["glob"])) for f in files):
            findings.append(f".cdx/test_routes.yaml: route glob matches no file: {route['glob']}")
    for glob, _local_only in long_suites(root):
        if not any(glob_match(f, glob) for f in files):
            findings.append(f".cdx/long_suites.txt: entry matches no file: {glob}")
    return findings


LINK = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")
BACKTICK = re.compile(r"`([^`\n]+)`")
FILE_LINE = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.\w{1,6}):(\d+)(?:-\d+)?\b")
YEARLESS = re.compile(r"(?<![\d./-])(?:0?[1-9]|1[0-2])-(?:0?[1-9]|[12]\d|3[01])(?![\d-])")
FULL_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
PATH_TOKEN = re.compile(r"[\w.-]+(?:/[\w.-]+)+")


def _resolves(token: str, root: Path, base: Path) -> bool:
    return (base / token).exists() or (root / token).exists()


def _index_findings(root: Path, project: dict) -> list[str]:
    findings: list[str] = []
    index_rel = project.get("docs_index") or ("docs/README.md" if (root / "docs/README.md").is_file() else None)
    if not index_rel or not (root / index_rel).is_file():
        return findings
    index = root / index_rel
    text = index.read_text(errors="replace")
    mentioned: set[str] = set()
    for line in text.splitlines():
        if not (line.lstrip().startswith(("|", "-", "*"))):
            continue
        tokens = [m for m in LINK.findall(line) if "://" not in m]
        tokens += [t for t in BACKTICK.findall(line) if re.fullmatch(r"[\w./-]+\.\w{1,6}", t)]
        for token in tokens:
            mentioned.add(Path(token).name)
            if not _resolves(token, root, index.parent):
                findings.append(f"{index_rel}: lists a file that does not exist: {token}")
    for path in sorted(index.parent.glob("*.md")):
        if path != index and path.name not in mentioned:
            findings.append(f"{index_rel}: {path.relative_to(root).as_posix()} is on disk but not in the index")
    return findings


def check_docs(root: Path) -> list[str]:
    project = load_project(root) if cdx_dir(root).is_dir() else {}
    findings = _index_findings(root, project)
    docs = [p for p in [root / "AGENTS.md", *sorted((root / "docs").rglob("*.md"))] if p.is_file()]
    for doc in docs:
        rel = doc.relative_to(root).as_posix()
        for number, line in enumerate(doc.read_text(errors="replace").splitlines(), 1):
            for path, lineno in FILE_LINE.findall(line):
                target = doc.parent / path if (doc.parent / path).is_file() else root / path
                if not target.is_file():
                    findings.append(f"{rel}:{number}: reference does not resolve: {path}:{lineno}")
                else:
                    count = len(target.read_text(errors="replace").splitlines())
                    if int(lineno) > count:
                        findings.append(f"{rel}:{number}: line out of range ({count} lines): {path}:{lineno}")
            for token in BACKTICK.findall(line):
                token = token.strip()
                if (not PATH_TOKEN.fullmatch(token) or token.startswith(("/", "~", "http")) or re.search(r"[{<*$]", token)
                        or not re.search(r"\.\w{1,6}$", token)):
                    continue
                if not _resolves(token, root, doc.parent):
                    findings.append(f"{rel}:{number}: path does not resolve: {token}")
    ledger_rel = project.get("ledger")
    if ledger_rel and (root / ledger_rel).is_file():
        for number, line in enumerate((root / ledger_rel).read_text(errors="replace").splitlines(), 1):
            if line.lstrip().startswith(("|", "-", "*")) and not FULL_DATE.search(line) and YEARLESS.search(line):
                findings.append(f"{ledger_rel}:{number}: ledger row date has no year: {line.strip()[:80]}")
    return findings


STAT_BOUND = re.compile(r"\b(?:max|p95|p99)\w*(?:\s+\w+){0,2}\W{0,3}(?:<=|≤)|(?:<=|≤)\W{0,3}(?:max|p95|p99)\b", re.I)
PERCENT = re.compile(r"\d(?:\.\d+)?\s*%|\brate\b\W+(?:of\W+)?0?\.\d+", re.I)
HAS_N = re.compile(r"\bn\s*=\s*\d|\d+\s*/\s*\d+|/\s*N\b")


def check_report(path: Path) -> list[str]:
    findings: list[str] = []
    lines = path.read_text(errors="replace").splitlines()
    name = path.name
    table: list[tuple[int, str]] = []

    def flush() -> None:
        if not table:
            return
        header = [c.strip().lower() for c in table[0][1].strip().strip("|").split("|")]
        has_method = any("method" in c or "baseline" in c for c in header)
        has_box = any("box" in c or "worlds" in c for c in header)
        if has_method and not has_box:
            findings.append(f"{name}:{table[0][0]}: comparison table has a method/baseline column but no box/worlds column")
        table.clear()

    for number, line in enumerate(lines, 1):
        if line.lstrip().startswith("|"):
            table.append((number, line))
        else:
            flush()
        if STAT_BOUND.search(line):
            findings.append(f"{name}:{number}: a measured max/p95/p99 is written as a bound (<= / ≤): {line.strip()[:80]}")
        is_rule = re.fullmatch(r"[\s|:-]*", line) is not None
        if not is_rule and PERCENT.search(line) and not HAS_N.search(line):
            findings.append(f"{name}:{number}: percentage or rate without n= or /N: {line.strip()[:80]}")
    flush()
    return findings


def check_refs(root: Path, phrase: str) -> list[str]:
    result = subprocess.run(["git", "-C", str(root), "grep", "-n", "-F", "--", phrase], capture_output=True, text=True, check=False)
    return [line for line in result.stdout.splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cdx_agent.check", description=__doc__)
    parser.add_argument("what", nargs="?", default="repo", choices=["repo", "docs", "report", "refs"])
    parser.add_argument("arg", nargs="?", help="FILE for report, PHRASE for refs")
    parser.add_argument("--repo", default=".")
    args = parser.parse_args(argv)
    root = Path(args.repo).resolve()
    if args.what in {"report", "refs"} and not args.arg:
        parser.error(f"{args.what} needs an argument")
    if args.what == "repo":
        findings = check_repo(root)
    elif args.what == "docs":
        findings = check_docs(root)
    elif args.what == "report":
        findings = check_report(Path(args.arg))
    else:
        findings = check_refs(root, args.arg)
    for finding in findings:
        print(finding)
    print(f"{len(findings)} finding(s)" if findings else "clean", file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
