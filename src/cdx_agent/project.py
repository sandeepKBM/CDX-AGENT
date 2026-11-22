"""Per-repo ``.cdx/`` conventions: project.json, test_routes.yaml, long_suites.txt.

A worktree without its own ``.cdx/`` falls back to the main checkout's, found through
the git common directory.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import yaml

PROJECT_KEYS = ("entry", "python", "ledger", "docs_index", "result_roots", "limit_sources",
                "sandbox_write", "graph_exclude")
DEFAULT_ROUTE = {"host_class": ["slurm-step", "slurm-job", "local"]}


def _common_root(repo: Path) -> Path | None:
    try:
        result = subprocess.run(["git", "-C", str(repo), "rev-parse", "--git-common-dir"],
                                capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    text = result.stdout.strip()
    if result.returncode != 0 or not text:
        return None
    common = Path(text)
    if not common.is_absolute():
        common = Path(repo) / common
    return common.resolve().parent


def cdx_dir(repo: str | Path) -> Path:
    """The repo's `.cdx/`; else the main checkout's when it exists; else the repo's (nonexistent) path."""
    repo = Path(repo)
    own = repo / ".cdx"
    if own.is_dir():
        return own
    main = _common_root(repo)
    if main is not None and (main / ".cdx").is_dir():
        return main / ".cdx"
    return own


def load_project(repo: str | Path) -> dict[str, Any]:
    path = cdx_dir(repo) / "project.json"
    if not path.is_file():
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return data


def load_routes(repo: str | Path) -> dict[str, Any]:
    """-> {"routes": [{glob, host_class, reason}], "default": {host_class: [...]}}."""
    path = cdx_dir(repo) / "test_routes.yaml"
    if not path.is_file():
        return {"routes": [], "default": dict(DEFAULT_ROUTE)}
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping")
    routes = data.get("routes") or []
    if not isinstance(routes, list):
        raise ValueError(f"{path}: routes must be a list")
    for route in routes:
        if not isinstance(route, dict) or "glob" not in route:
            raise ValueError(f"{path}: every route needs a glob: {route!r}")
        hosts = route.get("host_class")
        if isinstance(hosts, str):
            route["host_class"] = [hosts]
        elif not isinstance(hosts, list) or not hosts:
            raise ValueError(f"{path}: route {route['glob']!r} needs a host_class list")
    default = data.get("default") or dict(DEFAULT_ROUTE)
    if isinstance(default.get("host_class"), str):
        default["host_class"] = [default["host_class"]]
    return {"routes": routes, "default": default}


def long_suites(repo: str | Path) -> list[tuple[str, bool]]:
    """-> [(glob, local_only)] from `.cdx/long_suites.txt` (`glob [local-only]`, `#` comments)."""
    path = cdx_dir(repo) / "long_suites.txt"
    if not path.is_file():
        return []
    out: list[tuple[str, bool]] = []
    for line in path.read_text().splitlines():
        tokens = line.split("#", 1)[0].split()
        if not tokens:
            continue
        local_only = "local-only" in tokens[1:]
        glob = " ".join(t for i, t in enumerate(tokens) if i == 0 or t != "local-only")
        out.append((glob, local_only))
    return out


def load_resources(repo: str | Path) -> dict[str, Any]:
    path = cdx_dir(repo) / "resources.yaml"
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    return data if isinstance(data, dict) else {}
