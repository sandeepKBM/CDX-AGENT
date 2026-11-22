"""Routed per-file pytest fan-out: ``python -m cdx_agent.suite [PATHS] [--plan] [--jobs N] [--timeout-s S] [--out DIR]``.

Each test file is routed by ``.cdx/test_routes.yaml`` (first matching glob wins, else the default).
In this version only files whose host class allows this machine run (locally); the rest are
reported as ROUTED. States: PASS FAIL TIMEOUT ROUTING ROUTED. ROUTING means the failure names a
path under a prefix this host cannot read (a placement problem, not a test failure).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .project import load_resources, load_routes

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".tox", ".cdx"}
LOCAL_DATA_PREFIX = "/data/local"
ERROR_PATH = re.compile(r"(?:PermissionError|FileNotFoundError)[^\n]*?['\"](/[^'\"\n]+)['\"]")
TAIL_LINES = 25


def glob_match(rel: str, pattern: str) -> bool:
    """fnmatch-like with `**` crossing directories and `*`/`?` staying inside one."""
    out = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        out.append("[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c))
        i += 1
    return re.fullmatch("".join(out), rel) is not None


def host_tokens() -> set[str]:
    name = socket.gethostname().lower()
    tokens = {name, name.split(".")[0]}
    if "westeros" in name:
        tokens.add("westeros")
    return tokens


def is_westeros() -> bool:
    return "westeros" in host_tokens()


def collect(paths: list[str], root: Path) -> list[str]:
    files: set[str] = set()
    for item in paths or ["tests"]:
        target = Path(item)
        if not target.is_absolute():
            target = root / target
        if target.is_file():
            files.add(_rel(target, root))
        elif target.is_dir():
            for dirpath, dirnames, filenames in os.walk(target):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                for name in filenames:
                    if name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py")):
                        files.add(_rel(Path(dirpath) / name, root))
    return sorted(files)


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def route_for(rel: str, routes: dict[str, Any]) -> tuple[list[str], str]:
    for route in routes["routes"]:
        if glob_match(rel, str(route["glob"])):
            return list(route["host_class"]), str(route.get("reason") or "")
    return list(routes["default"].get("host_class") or ["local"]), ""


def can_run_here(host_class: list[str]) -> bool:
    return "local" in host_class or bool(set(host_class) & host_tokens())


def unreadable_prefixes(repo: Path) -> list[str]:
    prefixes = []
    data = load_resources(repo).get("data") or {}
    tokens = host_tokens()
    for prefix, spec in data.items():
        readable = (spec or {}).get("readable_from") if isinstance(spec, dict) else None
        if readable and not (set(readable) & tokens):
            prefixes.append(str(prefix))
    if not is_westeros():
        prefixes.append(LOCAL_DATA_PREFIX)
    return prefixes


def is_routing_failure(output: str, prefixes: list[str]) -> bool:
    for match in ERROR_PATH.finditer(output):
        path = match.group(1)
        if any(path == p or path.startswith(p.rstrip("/") + "/") for p in prefixes):
            return True
    return False


def run_one(rel: str, root: Path, timeout_s: float, prefixes: list[str]) -> dict[str, Any]:
    start = time.monotonic()
    proc = subprocess.Popen([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", rel], cwd=root,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        output, _ = proc.communicate(timeout=timeout_s)
        rc = proc.returncode
        state = "PASS" if rc == 0 else "FAIL"
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        output, _ = proc.communicate()
        rc, state = -9, "TIMEOUT"
    output = output or ""
    if state == "FAIL" and is_routing_failure(output, prefixes):
        state = "ROUTING"
    tail = "\n".join(output.splitlines()[-TAIL_LINES:])
    return {"file": rel, "state": state, "rc": rc, "seconds": round(time.monotonic() - start, 2), "tail": tail}


def plan_table(rows: list[tuple[str, list[str], str]]) -> str:
    width = max([len(r[0]) for r in rows] + [4])
    lines = [f"{'file'.ljust(width)}  host_class  reason"]
    for rel, hosts, reason in rows:
        lines.append(f"{rel.ljust(width)}  {','.join(hosts)}  {reason}".rstrip())
    return "\n".join(lines)


def scan(root: Path, paths: list[str]) -> list[str]:
    """Test files mentioning the local-data prefix, with proposed test_routes entries (print only)."""
    lines = []
    for rel in collect(paths, root):
        try:
            text = (root / rel).read_text(errors="replace")
        except OSError:
            continue
        if LOCAL_DATA_PREFIX in text:
            lines.append(f'  - {{glob: "{rel}", host_class: [westeros], reason: "mentions {LOCAL_DATA_PREFIX}"}}')
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cdx_agent.suite", description=__doc__)
    parser.add_argument("paths", nargs="*", help="files or directories (default tests/); first item `scan` lists /data/local tests")
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--timeout-s", type=float, default=600.0)
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    root = Path.cwd()

    if args.paths and args.paths[0] == "scan":
        entries = scan(root, args.paths[1:])
        if entries:
            print("# proposed .cdx/test_routes.yaml entries (not written)\nroutes:")
            print("\n".join(entries))
        else:
            print(f"no test files mention {LOCAL_DATA_PREFIX}")
        return 0

    routes = load_routes(root)
    files = collect(args.paths, root)
    plan = [(rel, *route_for(rel, routes)) for rel in files]
    if args.plan:
        print(plan_table(plan))
        return 0

    out_dir = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="cdx-suite-"))
    out_dir.mkdir(parents=True, exist_ok=True)
    prefixes = unreadable_prefixes(root)
    results: dict[str, dict[str, Any]] = {}
    runnable = []
    for rel, hosts, _reason in plan:
        if can_run_here(hosts):
            runnable.append((rel, hosts))
        else:
            print(f"{rel}: would route to {','.join(hosts)}; run via cdx-agent --run")
            results[rel] = {"file": rel, "state": "ROUTED", "rc": None, "seconds": 0.0, "tail": ""}
            results[rel]["host_class"] = hosts
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = [(rel, hosts, pool.submit(run_one, rel, root, args.timeout_s, prefixes)) for rel, hosts in runnable]
        for rel, hosts, future in futures:
            row = future.result()
            row["host_class"] = hosts
            results[rel] = row
            print(f"{row['state']:8s} {rel} ({row['seconds']}s)")
    ordered = [results[rel] for rel, _h, _r in plan]
    keys = ("file", "host_class", "state", "rc", "seconds", "tail")
    with (out_dir / "results.jsonl").open("w") as handle:
        for row in ordered:
            handle.write(json.dumps({k: row[k] for k in keys}) + "\n")
    counts: dict[str, int] = {}
    for row in ordered:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    print(f"results: {out_dir / 'results.jsonl'}  " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 1 if counts.get("FAIL") or counts.get("TIMEOUT") else 0


if __name__ == "__main__":
    raise SystemExit(main())
