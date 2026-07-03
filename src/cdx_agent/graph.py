"""Repository graph and workspace context builder used by CDX-AGENT."""

from __future__ import annotations

import argparse
import ast
import configparser
import importlib.metadata as importlib_metadata
import importlib.util as importlib_util
import json
import math
import os
import re
import subprocess
import sys
import warnings
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

try:  # Python 3.11+ ships tomllib; Python 3.10 uses tomli.
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10 only.
    import tomli as tomllib

try:
    import yaml
except Exception:  # pragma: no cover - yaml is expected to be available, but keep a soft fallback.
    yaml = None


SKIP_DIRS = {
    ".git",
    ".cache",
    ".conda",
    ".hg",
    ".sl",
    ".svn",
    ".env",
    ".venv",
    "anaconda3",
    "checkpoint",
    "checkpoints",
    "ckpts",
    "codex_logs",
    "codex_runtime",
    "conda",
    "data",
    "datasets",
    "env",
    "logs",
    "media",
    "miniconda3",
    "outputs",
    "runs",
    "venv",
    "videos",
    "wandb",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    "node_modules",
    ".codex",
    ".codex_graph",
    # Generated report/analysis artifacts: their JSON legitimately lists
    # hundreds of data paths, which floods config_edges with data lineage
    # that isn't config->code structure (observed: 1.1MB config_edges.json,
    # ~65% sourced from reports/).
    "reports",
}

RISKY_FOLDERS = (
    "checkpoints",
    "checkpoint",
    "datasets",
    "dataset",
    "data",
    "outputs",
    "logs",
    "wandb",
    "runs",
    "videos",
    "media",
    "ckpts",
    "codex_logs",
    "codex_runtime",
    ".git",
)

ENTRYPOINT_RE = re.compile(r"(train|eval|run|launch|collect|prepare|convert|export|serve)", re.IGNORECASE)
TASK_TOKEN_RE = re.compile(r"[A-Za-z0-9_./:-]+")
PATH_REF_RE = re.compile(r"([A-Za-z0-9_./-]+\.(?:ya?ml|json|toml|pt|pth|ckpt|safetensors|sh|py))")
TOPIC_HINTS = (
    "policy",
    "controller",
    "env",
    "training",
    "eval",
    "openpi",
    "openvla",
    "mujoco",
    "robosuite",
    "mjx",
    "dataset",
    "checkpoint",
    "rollout",
    "gripper",
)

HOME_ROOT = Path.home()
WORKSPACE_GRAPH_DIRNAME = ".codex_graph"
WORKSPACE_CONFIG_BASENAME = "workspace.yaml"
WORKSPACE_CONTEXT_PACK_BASENAME = "workspace_context_pack.md"
WORKSPACE_DEPENDENCY_REPOS_BASENAME = "dependency_repos.json"
WORKSPACE_DEPENDENCY_EDGES_BASENAME = "dependency_edges.json"
WORKSPACE_DETECT_PACKAGES = (
    "openpi",
    "deepreach",
    "robosuite",
    "mujoco",
    "lerobot",
    "openvla",
)
WORKSPACE_SPECIAL_REPO_NAMES = {
    "openpi": ("OpenPI", "openpi"),
    "deepreach": ("DeepReach", "deepreach"),
    "robosuite": ("robosuite", "Robosuite"),
    "mujoco": ("mujoco", "Mujoco"),
    "lerobot": ("LeRobot", "lerobot"),
    "openvla": ("OpenVLA", "openvla"),
}
WORKSPACE_DEFAULT_EXCLUDES = tuple(sorted({
    *SKIP_DIRS,
    "checkpoint",
    "checkpoints",
    "ckpts",
    "data",
    "dataset",
    "datasets",
    "logs",
    "outputs",
    "runs",
    "videos",
    "wandb",
    "media",
    "codex_logs",
    "codex_runtime",
    ".git",
    ".codex",
    ".codex_graph",
}))
WORKSPACE_INCLUDE_THIRD_PARTY_ENV = "CDX_AGENT_INCLUDE_THIRD_PARTY_DEPS"
WORKSPACE_INCLUDE_THIRD_PARTY_ENV_LEGACY = "CDX_WORKSPACE_INCLUDE_THIRD_PARTY_DEPS"
WORKSPACE_THIRD_PARTY_PATH_MARKERS = (
    "build_artifacts",
    "feedstock_root",
    "site-packages",
    "dist-packages",
    "conda-meta",
)
WORKSPACE_THIRD_PARTY_PATH_SUFFIXES = (".whl", ".egg", ".dist-info", ".egg-info")
SCAN_CACHE_BASENAME = ".scan_cache.json"
# Bump whenever FileNode's shape or the per-file analysis changes (e.g. E3
# added import_edges): the cache key is (mtime, size, kind), so an unchanged
# file's node from an OLDER analysis would otherwise be reused forever --
# observed live as a repo whose nodes permanently lacked import_edges.
SCAN_CACHE_SCHEMA_VERSION = 2


@dataclass
class FileNode:
    path: str
    kind: str
    imports: list[str]
    classes: list[str]
    functions: list[str]
    tags: list[str]
    path_refs: list[str]
    import_edges: list[dict[str, Any]] = field(default_factory=list)


def _project_markers(repo: Path) -> list[Path]:
    markers = (
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "requirements.txt",
        "package.json",
        "Cargo.toml",
        "AGENTS.md",
    )
    return [repo / marker for marker in markers]


def _repo_root(repo: Path) -> Path:
    repo = repo.resolve()
    probe = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if probe.returncode == 0:
        return Path(probe.stdout.strip()).resolve()
    return repo


def _is_home_like_dir(path: Path) -> bool:
    # Ancestor check, not exact-match-only: a subdirectory of home (or a
    # symlink resolving into it) is just as unsafe to bulk-scan as home
    # itself. Matches the fix applied to config.py::is_home_like_dir.
    try:
        resolved = path.resolve()
        home = HOME_ROOT.resolve()
    except OSError:
        return False
    return resolved == home or home in resolved.parents


def resolve_repo_root(repo_arg: str, force_home_scan: bool = False) -> Path:
    repo = _repo_root(Path(repo_arg))
    if _is_home_like_dir(repo) and not force_home_scan:
        raise ValueError(
            "Refusing to scan a home directory. Pass --force-home-scan if you really intend that scan."
        )
    return repo


def _looks_like_project_dir(repo: Path) -> bool:
    return any(marker.exists() for marker in _project_markers(repo))


# Upper bound on how many files a single scan will ever walk, independent of
# `max_files`. Exists so prioritized truncation (below) doesn't turn into an
# unbounded walk on a pathologically large tree.
SCAN_HARD_CEILING = 200_000


def _file_priority_key(path: Path, repo: Path) -> tuple[int, int, float, str]:
    """Lower sorts first. Prefers entrypoint-name-like files, then shallower
    paths, then most-recently-modified, so truncation (when the scan exceeds
    `max_files`) is more likely to keep the files that actually matter instead
    of whatever `os.walk` happened to reach first."""
    try:
        rel = path.relative_to(repo)
    except ValueError:
        rel = path
    depth = len(rel.parts)
    entrypoint_rank = 0 if ENTRYPOINT_RE.search(path.name) else 1
    try:
        mtime_rank = -path.stat().st_mtime
    except OSError:
        mtime_rank = 0.0
    return (entrypoint_rank, depth, mtime_rank, str(rel))


def _iter_files(
    repo: Path,
    max_files: int,
    skip_dirs: set[str] | None = None,
) -> tuple[list[Path], bool, list[Path]]:
    """Returns (kept_files, truncated, dropped_sample). `dropped_sample` is a
    representative slice (not exhaustive) of files cut by truncation, so
    callers can report *what* was dropped instead of just *that* something
    was dropped."""
    all_files: list[Path] = []
    blocked_dirs = set(SKIP_DIRS)
    if skip_dirs:
        blocked_dirs.update(skip_dirs)
    hit_hard_ceiling = False
    for root, dirs, filenames in os.walk(repo):
        root_path = Path(root)
        # A subdirectory carrying its own .git is a nested/vendored repo
        # (mujoco_menagerie, coppelia runtimes, ...) -- its internals belong
        # to that repo's own graph, and scanning it here only produces the
        # vendored-noise configs/parse-errors observed in real packs.
        dirs[:] = [
            d for d in dirs if d not in blocked_dirs and not (root_path / d / ".git").exists()
        ]
        for filename in filenames:
            all_files.append(root_path / filename)
            if len(all_files) >= SCAN_HARD_CEILING:
                hit_hard_ceiling = True
                break
        if hit_hard_ceiling:
            break

    if len(all_files) <= max_files and not hit_hard_ceiling:
        return all_files, False, []

    ranked = sorted(all_files, key=lambda p: _file_priority_key(p, repo))
    kept = ranked[:max_files]
    dropped = ranked[max_files:]
    kept_set = set(kept)
    # Preserve original walk order for kept files so downstream node/edge
    # ordering stays stable and predictable, prioritization only decides
    # *which* files survive the cap, not the order they're processed in.
    ordered_kept = [p for p in all_files if p in kept_set]
    return ordered_kept, True, dropped[:50]


def _tags_from_text(text: str, hints: Iterable[str] = TOPIC_HINTS) -> list[str]:
    lower = text.lower()
    return sorted({hint for hint in hints if hint in lower})


def _discover_topic_hints_from_repo(repo: Path) -> set[str]:
    """Cheap, domain-agnostic discovery fallback: pulls `[project].keywords`
    and top-level local package/module names out of pyproject.toml, so a
    repo with no explicit `topic_hints` override in workspace.yaml still gets
    some non-generic tags instead of only ever matching the shipped
    robotics-flavored defaults (E2 fix)."""
    discovered: set[str] = set()
    pyproject = repo / "pyproject.toml"
    if pyproject.exists():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        project = data.get("project") if isinstance(data, dict) else {}
        if isinstance(project, dict):
            keywords = project.get("keywords")
            if isinstance(keywords, list):
                for keyword in keywords:
                    if isinstance(keyword, str) and keyword.strip():
                        discovered.add(_normalize_name(keyword.strip()))
    for parent in (repo, repo / "src"):
        if not parent.is_dir():
            continue
        for child in parent.iterdir():
            if child.is_dir() and (child / "__init__.py").exists():
                discovered.add(_normalize_name(child.name))
    return {hint for hint in discovered if hint}


def resolve_topic_hints(repo: Path) -> tuple[str, ...]:
    """Topic hints used to tag scanned files (see `_tags_from_text`).
    Precedence: an explicit `topic_hints` list in workspace.yaml if present,
    else the shipped defaults (`TOPIC_HINTS`) merged with a cheap discovery
    pass over the repo's own pyproject.toml (E2 fix -- previously this was
    only ever the fixed, robotics-flavored constant tuple with no override or
    discovery mechanism)."""
    try:
        payload = _workspace_read_yaml(_workspace_config_path(repo))
    except (RuntimeError, ValueError):
        payload = {}
    override = payload.get("topic_hints") if isinstance(payload, dict) else None
    if isinstance(override, list) and override:
        return tuple(sorted({_normalize_name(str(item)) for item in override if str(item).strip()}))
    return tuple(sorted(set(TOPIC_HINTS) | _discover_topic_hints_from_repo(repo)))


def _path_refs_from_text(text: str) -> list[str]:
    return sorted(set(PATH_REF_RE.findall(text)))


def _parse_python_ast(path: Path) -> tuple[ast.AST | None, list[dict[str, Any]], str]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(text, filename=str(path))
        return tree, [], text
    except SyntaxError as exc:
        error = {
            "file": str(path),
            "line": exc.lineno,
            "message": exc.msg,
            "type": "SyntaxError",
        }
        return None, [error], text


def _package_roots(repo: Path) -> list[Path]:
    """Candidate import roots to resolve absolute imports against: the repo
    root itself, plus `src/` if it exists (the common src-layout convention
    where `import mypackage` really means `src/mypackage`)."""
    roots = [repo]
    src = repo / "src"
    if src.is_dir():
        roots.append(src)
    return roots


def _resolve_module_to_file(root: Path, dotted: str) -> Path | None:
    """Resolve a dotted module path like 'foo.bar' to a concrete file under
    `root`, trying both the module-as-file and module-as-package forms."""
    parts = [p for p in dotted.split(".") if p]
    if not parts:
        return None
    candidate = root.joinpath(*parts)
    py_file = candidate.with_suffix(".py")
    if py_file.is_file():
        return py_file
    init_file = candidate / "__init__.py"
    if init_file.is_file():
        return init_file
    return None


def _resolve_absolute_import(repo: Path, module: str, imported_name: str | None) -> Path | None:
    # For `from X import Y`, prefer treating Y as a submodule of X
    # (`X/Y.py`) first -- that's the primary intent of `from pkg import
    # submodule` -- and only fall back to X itself (Y is an attribute/name
    # defined inside X's __init__.py, not a submodule) if that doesn't
    # resolve. Trying X first would always "win" whenever X is a real
    # package, even when Y is clearly meant as a submodule.
    candidates = []
    if imported_name:
        candidates.append(f"{module}.{imported_name}" if module else imported_name)
    if module:
        candidates.append(module)
    for root in _package_roots(repo):
        for candidate in candidates:
            resolved = _resolve_module_to_file(root, candidate)
            if resolved is not None:
                return resolved
    return None


def _resolve_relative_base(repo: Path, file_path: Path, level: int) -> Path | None:
    """For `from . import x` (level=1), `from .. import x` (level=2), etc,
    compute the package directory the leading dots refer to, relative to
    `file_path`'s own location (approximates the file's own directory as its
    package -- accurate for the common case of a non-package-root module)."""
    base = file_path.parent
    for _ in range(max(level - 1, 0)):
        base = base.parent
    resolved_repo = repo.resolve()
    try:
        base.resolve().relative_to(resolved_repo)
    except ValueError:
        return None
    return base


def _resolve_relative_import(
    repo: Path, file_path: Path, module: str | None, level: int, imported_name: str | None
) -> Path | None:
    base = _resolve_relative_base(repo, file_path, level)
    if base is None:
        return None
    # Same submodule-before-package-init preference as _resolve_absolute_import.
    candidates = []
    if module and imported_name:
        candidates.append(f"{module}.{imported_name}")
    elif imported_name:
        candidates.append(imported_name)
    if module:
        candidates.append(module)
    for candidate in candidates:
        resolved = _resolve_module_to_file(base, candidate)
        if resolved is not None:
            return resolved
    return None


def _import_edge_record(repo: Path, raw: str, target: Path | None) -> dict[str, Any]:
    resolved_repo = repo.resolve()
    target_rel = None
    if target is not None:
        try:
            target_rel = str(target.resolve().relative_to(resolved_repo))
        except (OSError, ValueError):
            target_rel = None
    return {"raw": raw, "target": target_rel, "resolved": target_rel is not None}


def _import_edges_from_ast(tree: ast.AST, repo: Path, file_path: Path) -> list[dict[str, Any]]:
    """Real import resolution (E3 fix): previously the graph only recorded
    bare import-name strings with no verification they resolve to a real
    file, which was ambiguous for same-named modules in different packages.
    This walks package roots / relative-import levels to resolve each import
    to a concrete file within the repo where possible, marking it
    unresolved (likely a third-party/external import) otherwise."""
    edges: list[dict[str, Any]] = []
    seen: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                raw = alias.name
                if raw in seen:
                    continue
                seen.add(raw)
                target = _resolve_absolute_import(repo, raw, None)
                edges.append(_import_edge_record(repo, raw, target))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            level = node.level or 0
            for alias in node.names:
                imported_name = None if alias.name == "*" else alias.name
                raw = ("." * level) + module + (f".{imported_name}" if imported_name else "")
                if raw in seen:
                    continue
                seen.add(raw)
                if level > 0:
                    target = _resolve_relative_import(repo, file_path, module or None, level, imported_name)
                else:
                    target = _resolve_absolute_import(repo, module, imported_name)
                edges.append(_import_edge_record(repo, raw, target))
    return edges


def _scan_python(path: Path, repo: Path, hints: Iterable[str] = TOPIC_HINTS) -> tuple[FileNode, list[dict[str, Any]]]:
    tree, parse_errors, text = _parse_python_ast(path)
    imports: list[str] = []
    functions: list[str] = []
    classes: list[str] = []
    if tree is None:
        return (
            FileNode(
                path=str(path.relative_to(repo)),
                kind="python",
                imports=[],
                classes=[],
                functions=[],
                tags=_tags_from_text(text, hints),
                path_refs=_path_refs_from_text(text),
            ),
            parse_errors,
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module)
        elif isinstance(node, ast.FunctionDef):
            functions.append(node.name)
        elif isinstance(node, ast.AsyncFunctionDef):
            functions.append(node.name)
        elif isinstance(node, ast.ClassDef):
            classes.append(node.name)
    import_edges = _import_edges_from_ast(tree, repo, path)
    return (
        FileNode(
            path=str(path.relative_to(repo)),
            kind="python",
            imports=sorted(set(imports)),
            classes=sorted(set(classes)),
            functions=sorted(set(functions)),
            tags=_tags_from_text(text, hints),
            path_refs=_path_refs_from_text(text),
            import_edges=import_edges,
        ),
        parse_errors,
    )


def _scan_textual(path: Path, repo: Path, kind: str, hints: Iterable[str] = TOPIC_HINTS) -> FileNode:
    text = path.read_text(encoding="utf-8", errors="ignore")
    return FileNode(
        path=str(path.relative_to(repo)),
        kind=kind,
        imports=[],
        classes=[],
        functions=[],
        tags=_tags_from_text(text, hints),
        path_refs=_path_refs_from_text(text),
    )


_DOCKERFILE_NAME_RE = re.compile(r"^(dockerfile|.*\.dockerfile)$", re.IGNORECASE)


def _classify_file(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix == ".py":
        return "python"
    if suffix in {".sh", ".bash"} or path.name.startswith("run_") or path.name.startswith("launch_"):
        return "shell"
    if suffix in {".yaml", ".yml"}:
        return "yaml"
    if suffix == ".json":
        return "json"
    if suffix == ".toml":
        return "toml"
    if _DOCKERFILE_NAME_RE.match(path.name):
        # E4: cheap, high-signal for entrypoint detection (CMD/ENTRYPOINT
        # usually names the real launcher). Reuses the existing generic
        # textual scan (tags + path_refs), no dedicated parser needed.
        return "dockerfile"
    return None


_PYTHON_INVOCATION_RE = re.compile(r"\bpython3?\s+(?:-m\s+)?([A-Za-z0-9_./-]+\.py)\b")
_CONSOLE_SCRIPT_ENTRY_RE = re.compile(r"[\"']?([\w.-]+)[\"']?\s*=\s*([\w.]+):(\w+)")


def _add_declared(declared: dict[str, str], target: Path | None, repo: Path, reason: str) -> None:
    if target is None:
        return
    try:
        rel = str(target.resolve().relative_to(repo.resolve()))
    except (OSError, ValueError):
        return
    declared.setdefault(rel, reason)


def _discover_pyproject_scripts(repo: Path, declared: dict[str, str]) -> None:
    pyproject = repo / "pyproject.toml"
    if not pyproject.is_file():
        return
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError):
        return
    project = data.get("project", {}) if isinstance(data, dict) else {}
    scripts = project.get("scripts", {}) if isinstance(project, dict) else {}
    if not isinstance(scripts, dict):
        return
    for name, target_spec in scripts.items():
        if not isinstance(target_spec, str) or ":" not in target_spec:
            continue
        module = target_spec.split(":", 1)[0].strip()
        target = _resolve_absolute_import(repo, module, None)
        _add_declared(declared, target, repo, f"pyproject.toml [project.scripts] '{name}'")


def _discover_setup_py_console_scripts(repo: Path, declared: dict[str, str]) -> None:
    setup_py = repo / "setup.py"
    if not setup_py.is_file():
        return
    try:
        text = setup_py.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return
    if "console_scripts" not in text:
        return
    # Best-effort: regex over the whole file rather than executing/AST-parsing
    # setup.py (which can run arbitrary code) -- good enough for the common
    # `"name = module:func"` entry_points string format.
    idx = text.find("console_scripts")
    window = text[idx : idx + 2000]
    for match in _CONSOLE_SCRIPT_ENTRY_RE.finditer(window):
        name, module, _func = match.groups()
        target = _resolve_absolute_import(repo, module, None)
        _add_declared(declared, target, repo, f"setup.py console_scripts '{name}'")


def _discover_python_invocations_in_file(repo: Path, path: Path, reason_prefix: str, declared: dict[str, str]) -> None:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return
    for match in _PYTHON_INVOCATION_RE.finditer(text):
        ref = match.group(1)
        candidate = repo / ref
        if candidate.is_file():
            _add_declared(declared, candidate, repo, f"{reason_prefix} ({path.name})")


def _discover_makefile_targets(repo: Path, declared: dict[str, str]) -> None:
    for name in ("Makefile", "makefile", "GNUmakefile"):
        makefile = repo / name
        if makefile.is_file():
            _discover_python_invocations_in_file(repo, makefile, "Makefile target", declared)


def _discover_ci_run_steps(repo: Path, declared: dict[str, str]) -> None:
    ci_paths: list[Path] = []
    workflows_dir = repo / ".github" / "workflows"
    if workflows_dir.is_dir():
        ci_paths.extend(sorted(workflows_dir.glob("*.yml")) + sorted(workflows_dir.glob("*.yaml")))
    for name in (".gitlab-ci.yml", ".gitlab-ci.yaml"):
        candidate = repo / name
        if candidate.is_file():
            ci_paths.append(candidate)
    for ci_path in ci_paths:
        _discover_python_invocations_in_file(repo, ci_path, "CI run step", declared)


def _discover_declared_entrypoints(repo: Path) -> dict[str, str]:
    """E6 fix: previously entrypoint detection was purely filename-keyword
    regex matching. This gives real project-declared entrypoints -- what
    pyproject.toml/setup.py actually ship as a command, what the Makefile
    and CI actually invoke -- much stronger signal than a name pattern."""
    declared: dict[str, str] = {}
    _discover_pyproject_scripts(repo, declared)
    _discover_setup_py_console_scripts(repo, declared)
    _discover_makefile_targets(repo, declared)
    _discover_ci_run_steps(repo, declared)
    return declared


def _likely_entrypoints(nodes: list[FileNode], repo: Path | None = None) -> list[dict[str, Any]]:
    declared = _discover_declared_entrypoints(repo) if repo is not None else {}
    out: list[dict[str, Any]] = []
    for node in nodes:
        score = 0
        declared_reason = declared.get(node.path)
        if declared_reason is not None:
            score += 6  # outweighs filename heuristics -- this is a real, tooling-declared entrypoint
        if ENTRYPOINT_RE.search(Path(node.path).name):
            score += 3
        if node.kind == "python" and "main" in node.functions:
            score += 2
        if "openpi" in node.tags or "openvla" in node.tags:
            score += 1
        if score > 0:
            entry: dict[str, Any] = {"path": node.path, "kind": node.kind, "score": score, "tags": node.tags}
            if declared_reason is not None:
                entry["declared_by"] = declared_reason
            out.append(entry)
    return sorted(out, key=lambda item: (-int(item["score"]), item["path"]))[:30]


def _resolve_path_ref(repo: Path, source_dir: Path, ref: str) -> str | None:
    """Resolve a regex-detected path-looking string to a real file, relative
    to the repo root or to the referencing file's own directory. Returns None
    (drop the edge) if it doesn't resolve to anything on disk -- fix for E3's
    `_config_edges` half: previously every regex match was kept regardless of
    whether it pointed at a real file."""
    ref_path = Path(ref)
    resolved_repo = repo.resolve()
    candidates = [ref_path] if ref_path.is_absolute() else [repo / ref_path, source_dir / ref_path]
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            return str(candidate.resolve().relative_to(resolved_repo))
        except (OSError, ValueError):
            continue
    return None


MAX_CONFIG_EDGES_PER_SOURCE = 10
CONFIG_EDGES_ARTIFACT_CAP = 2000

_CONFIG_NOISE_NAME_RE = re.compile(r"(report|manifest|summary|matrix|lock)", re.IGNORECASE)


def _config_edges(nodes: list[FileNode], repo: Path) -> list[dict[str, Any]]:
    per_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    for node in nodes:
        # A .json whose NAME already says report/manifest/lockfile is a data
        # artifact; the paths it lists are lineage, not config->code wiring.
        if node.kind == "json" and _CONFIG_NOISE_NAME_RE.search(Path(node.path).name):
            continue
        source_dir = (repo / node.path).parent
        for ref in node.path_refs:
            if not ref.endswith((".yaml", ".yml", ".json", ".toml")):
                continue
            target = _resolve_path_ref(repo, source_dir, ref)
            if target is None:
                continue
            key = (node.path, target)
            if key in seen:
                continue
            seen.add(key)
            per_source[node.path].append({"source": node.path, "target": target})
    edges: list[dict[str, Any]] = []
    for source in sorted(per_source):
        items = per_source[source]
        if len(items) > MAX_CONFIG_EDGES_PER_SOURCE:
            # A single "config" emitting dozens of edges is a data manifest
            # enumerating datasets/results, not configuration structure.
            continue
        edges.extend(items)
    return edges


def _reverse_import_index(nodes: list[FileNode]) -> dict[str, list[str]]:
    """Bare-module-name-keyed index, kept only for backward compatibility
    with existing consumers of this exact key in repo_graph.json. Ambiguous
    for same-named modules in different packages -- prefer
    `_file_reverse_import_index` (file-path-keyed, only resolved edges) for
    anything new, notably `--impact`."""
    reverse: dict[str, list[str]] = defaultdict(list)
    for node in nodes:
        module_key = node.path.removesuffix(".py").replace("/", ".")
        for imported in node.imports:
            reverse[imported].append(node.path)
            if imported.startswith(module_key):
                reverse[imported].append(node.path)
    return {key: sorted(set(value)) for key, value in reverse.items()}


def _file_reverse_import_index(nodes: list[FileNode]) -> dict[str, list[str]]:
    """Maps a repo-relative FILE PATH (not a bare module name) to the files
    that resolvably import it (E3 fix). Only edges that resolved to a real
    file in the repo are included, so this never conflates two same-named
    modules living in different packages the way the bare-name index could."""
    reverse: dict[str, list[str]] = defaultdict(list)
    for node in nodes:
        for edge in node.import_edges:
            target = edge.get("target")
            if target:
                reverse[target].append(node.path)
    return {key: sorted(set(value)) for key, value in reverse.items()}


def _task_tokens(task: str) -> list[str]:
    return [token.lower() for token in TASK_TOKEN_RE.findall(task)]


def _node_haystack(node: FileNode) -> str:
    return f"{node.path} {' '.join(node.tags)} {' '.join(node.imports)}".lower()


def _document_frequencies(nodes: list[FileNode], task_tokens: list[str]) -> dict[str, int]:
    """How many nodes each task token appears in at least once -- the "DF"
    half of a TF-IDF-style weighting (E7 fix). A token that shows up in
    nearly every file in the repo (e.g. "train" in a training-heavy repo)
    is a weak discriminator and should count for much less than a token
    that appears in only a handful of files."""
    token_set = set(task_tokens)
    freqs = dict.fromkeys(token_set, 0)
    for node in nodes:
        haystack = _node_haystack(node)
        for token in token_set:
            if token in haystack:
                freqs[token] += 1
    return freqs


def _score_node_for_task(
    node: FileNode,
    task_tokens: list[str],
    doc_freqs: dict[str, int] | None = None,
    total_nodes: int = 0,
) -> float:
    haystacks = [node.path.lower(), " ".join(node.tags).lower(), " ".join(node.imports).lower()]
    name = Path(node.path).name.lower()
    score = 0.0
    for token in task_tokens:
        # Smoothed idf: always >= ~1.0 so behavior degrades gracefully to the
        # original flat weighting when doc_freqs isn't supplied (df=0 case).
        idf = 1.0
        if doc_freqs is not None and total_nodes > 0:
            df = doc_freqs.get(token, 0)
            idf = math.log((total_nodes + 1) / (df + 1)) + 1.0
        for haystack in haystacks:
            if token in haystack:
                score += 2 * idf
        if token in name:
            score += 3 * idf
    if ENTRYPOINT_RE.search(node.path):
        score += 1
    return score


def _suggested_checks(nodes: list[FileNode]) -> list[str]:
    joined = " ".join(node.path.lower() for node in nodes)
    checks = ["validate YAML/JSON/TOML parsing for touched configs"]
    if any("train" in node.path.lower() for node in nodes):
        checks.append("run a one-batch or one-step training smoke test before long training")
    if any(any(tag in node.tags for tag in ("policy", "controller", "env")) for node in nodes):
        checks.append("run a short rollout or policy-wrapper smoke test before full evaluation")
    if "openpi" in joined:
        checks.append("verify checkpoint path, norm stats, action horizon, and first-7 action mapping")
    if "openvla" in joined:
        checks.append("verify unnorm_key, rollout_adapter sidecars, and action conversion before rollout")
    return checks


def _import_call_chains(
    nodes: list[FileNode],
    entrypoints: list[dict[str, Any]],
    max_depth: int = 3,
    max_chains: int = 12,
) -> list[str]:
    """Real call-chain candidates: walk the E3-resolved `import_edges` from
    the top entrypoints (depth-bounded DFS) and render `entry -> module ->
    module` chains. Replaces the old path-substring bucketing, which never
    consulted a single edge and was decoration rather than structure."""
    by_path = {node.path: node for node in nodes}
    chains: list[str] = []
    productive_entries = 0
    for item in entrypoints:
        if productive_entries >= 5 or len(chains) >= max_chains:
            break
        start = item["path"]
        node = by_path.get(start)
        if node is None:
            continue
        # Launcher-style scripts (stdlib + runpy/subprocess only) have no
        # resolved local imports; skip them instead of burning the entrypoint
        # budget on entries that can't produce a chain.
        if not any(edge.get("target") for edge in node.import_edges):
            continue
        productive_entries += 1
        emitted_for_entry = 0
        stack: list[list[str]] = [[start]]
        while stack and emitted_for_entry < 4 and len(chains) < max_chains:
            path_so_far = stack.pop()
            current = by_path.get(path_so_far[-1])
            targets: list[str] = []
            if current is not None and len(path_so_far) <= max_depth:
                # dict.fromkeys: several import statements can resolve to the
                # same target file; without dedupe every duplicate edge spawns
                # an identical chain.
                targets = list(
                    dict.fromkeys(
                        edge.get("target")
                        for edge in current.import_edges
                        if edge.get("target") and edge.get("target") not in path_so_far
                    )
                )
            if targets:
                for target in targets[:4]:
                    stack.append(path_so_far + [target])
            elif len(path_so_far) > 1:
                rendered = " -> ".join(f"`{p}`" for p in path_so_far)
                if rendered not in chains:
                    chains.append(rendered)
                    emitted_for_entry += 1
    return chains


def _possible_call_chain(nodes: list[FileNode], entrypoints: list[dict[str, Any]]) -> list[str]:
    """Fallback grouping for repos where no entrypoint has resolved import
    edges (e.g. pure-script repos) -- prefer `_import_call_chains`."""
    chain: list[str] = []
    entry_paths = [item["path"] for item in entrypoints[:8]]
    policy_paths = [node.path for node in nodes if "/policies/" in f"/{node.path}" or "policy" in node.path.lower()]
    env_paths = [node.path for node in nodes if "/env/" in f"/{node.path}" or "robosuite" in node.path.lower()]
    runtime_paths = [node.path for node in nodes if "/runtime/" in f"/{node.path}" or "runner.py" in node.path.lower()]
    if entry_paths:
        chain.append("entrypoints: " + ", ".join(entry_paths[:5]))
    if runtime_paths:
        chain.append("runtime: " + ", ".join(runtime_paths[:3]))
    if policy_paths:
        chain.append("policies: " + ", ".join(policy_paths[:5]))
    if env_paths:
        chain.append("env: " + ", ".join(env_paths[:3]))
    return chain


_CONFIG_NOISE_DIRS = {"third_party", "external", "vendor", "vendors", "reports", "docs"}


def _ranked_configs(nodes: list[FileNode], config_edges: list[dict[str, Any]]) -> list[str]:
    """Rank config files by how likely an agent actually needs them, instead
    of the old first-30-in-walk-order slice that led with vendored manual
    indexes and generated report JSON while burying `configs/*.yaml`."""
    inbound: dict[str, int] = defaultdict(int)
    for edge in config_edges:
        inbound[edge["target"]] += 1
    scored: list[tuple[float, str]] = []
    for node in nodes:
        if node.kind not in {"yaml", "json", "toml"}:
            continue
        parts = Path(node.path).parts
        name = parts[-1]
        score = 0.0
        if any(part in _CONFIG_NOISE_DIRS for part in parts[:-1]):
            score -= 10
        if parts[0] in {"config", "configs"} or (len(parts) > 1 and parts[-2] in {"config", "configs"}):
            score += 5
        if len(parts) == 1 and node.kind in {"yaml", "toml"}:
            score += 4  # repo-root yaml/toml is almost always real config
        if name == "pyproject.toml":
            score += 5
        if _CONFIG_NOISE_NAME_RE.search(name):
            score -= 6
        score += min(inbound.get(node.path, 0), 5)  # referenced by code = real
        scored.append((score, node.path))
    scored.sort(key=lambda item: (-item[0], item[1]))
    positive = [path for score, path in scored if score > 0]
    if positive:
        return positive[:15]
    return [path for _, path in scored[:8]]


def _build_context_pack(
    repo: Path, nodes: list[FileNode], task: str, config_edges: list[dict[str, Any]] | None = None
) -> str:
    task_tokens = _task_tokens(task)
    entrypoints = _likely_entrypoints(nodes, repo)
    if config_edges is None:
        config_edges = _config_edges(nodes, repo)
    relevance_note = ""
    if task_tokens:
        doc_freqs = _document_frequencies(nodes, task_tokens)
        scored = sorted(
            (
                {
                    "path": node.path,
                    "score": _score_node_for_task(node, task_tokens, doc_freqs, len(nodes)),
                    "tags": node.tags,
                }
                for node in nodes
            ),
            key=lambda item: (-item["score"], item["path"]),
        )
        relevant = [item for item in scored if item["score"] > 0][:20]
    else:
        # Without a task, token scoring degenerates to a +1 for
        # entrypoint-looking filenames -- a noisy duplicate of the
        # entrypoints section. Rank by structural signal instead.
        reverse = _file_reverse_import_index(nodes)
        scored = sorted(
            (
                {
                    "path": node.path,
                    "score": 2 * len(node.tags) + min(len(reverse.get(node.path, [])), 5),
                    "tags": node.tags,
                }
                for node in nodes
            ),
            key=lambda item: (-item["score"], item["path"]),
        )
        relevant = [item for item in scored if item["score"] > 0][:15]
        relevance_note = "(no task hint: ranked by topic tags + inbound imports; pass --task for task relevance)"
    configs = _ranked_configs(nodes, config_edges)
    risky = [part for part in RISKY_FOLDERS if (repo / part).exists()]
    lines = [
        f"# Context Pack for {repo.name}",
        "",
        "## Repo summary",
        f"- Root: `{repo}`",
        f"- Python/config/script files indexed: `{len(nodes)}`",
        f"- Task hint: `{task or 'none'}`",
        f"- Generated: `{datetime.now().isoformat(timespec='seconds')}` (pack format v2; regenerate with `cdx-agent --graph` when stale)",
        "",
        "## Likely entrypoints",
    ]
    lines.extend(f"- `{item['path']}` score={item['score']} tags={','.join(item['tags'])}" for item in entrypoints[:12])
    lines.extend(["", "## Important configs"])
    lines.extend(f"- `{path}`" for path in configs)
    lines.extend(["", "## Risky folders not to edit"])
    if risky:
        lines.extend(f"- `{name}`" for name in risky)
    else:
        lines.append("- `.git`, checkpoints, datasets, outputs, logs, and other generated artifacts")
    lines.extend(["", "## Task-relevant files"])
    if relevance_note:
        lines.append(f"- {relevance_note}")
    if relevant:
        lines.extend(f"- `{item['path']}` score={item['score']} tags={','.join(item['tags'])}" for item in relevant)
    else:
        lines.append("- No strong task matches yet; refine the task string or inspect entrypoints first.")
    lines.extend(["", "## Possible call chain"])
    chains = _import_call_chains(nodes, entrypoints)
    if not chains:
        chains = _possible_call_chain(nodes, entrypoints)
    lines.extend(f"- {item}" for item in chains)
    lines.extend(["", "## Suggested tests or smoke checks"])
    lines.extend(f"- {item}" for item in _suggested_checks(nodes))
    lines.extend(["", "## Questions Codex must answer before editing"])
    lines.extend(
        [
            "- What is the active entrypoint for this task?",
            "- Which config or YAML controls the behavior being changed?",
            "- What is the policy/controller/env/training/eval call chain?",
            "- Which checkpoint, dataset, and log paths are active?",
            "- What is the smallest safe smoke test after the edit?",
        ]
    )
    return "\n".join(lines) + "\n"


def _scan_cache_path(repo: Path) -> Path:
    return repo / WORKSPACE_GRAPH_DIRNAME / SCAN_CACHE_BASENAME


def pack_staleness(repo: Path) -> str | None:
    """None if the repo's context_pack.md is absent or up to date; otherwise
    a human-readable reason it looks stale. Cheap: stats the files the last
    scan already indexed (no fresh walk) plus one `git log -1`."""
    pack = repo / WORKSPACE_GRAPH_DIRNAME / "context_pack.md"
    if not pack.is_file():
        return None
    pack_mtime = pack.stat().st_mtime
    reasons: list[str] = []
    cache_path = _scan_cache_path(repo)
    if cache_path.is_file():
        try:
            entries = json.loads(cache_path.read_text(encoding="utf-8")).get("entries", {})
        except (json.JSONDecodeError, OSError):
            entries = {}
        newer = 0
        for rel in list(entries)[:5000]:
            try:
                if (repo / rel).stat().st_mtime > pack_mtime:
                    newer += 1
            except OSError:
                continue
        if newer:
            reasons.append(f"{newer} indexed source file(s) modified since the pack was generated")
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--format=%ct"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip() and int(result.stdout.strip()) > pack_mtime:
            reasons.append("commits landed after the pack was generated")
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass
    if not reasons:
        return None
    return "context_pack.md looks STALE (" + "; ".join(reasons) + ") -- rerun `cdx-agent --graph`"


def _load_scan_cache(repo: Path, hints: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    cache_path = _scan_cache_path(repo)
    if not cache_path.is_file():
        return {}
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(payload, dict):
        return {}
    # Tags depend on topic_hints; a hints change invalidates every cached
    # entry at once rather than risking stale tags on a cache hit. Same for
    # the schema version -- an entry produced by older analysis code is
    # wrong even though the file itself hasn't changed.
    if payload.get("schema_version") != SCAN_CACHE_SCHEMA_VERSION:
        return {}
    if list(payload.get("hints", [])) != list(hints):
        return {}
    entries = payload.get("entries")
    return entries if isinstance(entries, dict) else {}


def _save_scan_cache(repo: Path, hints: tuple[str, ...], entries: dict[str, dict[str, Any]]) -> None:
    cache_path = _scan_cache_path(repo)
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(
                {"schema_version": SCAN_CACHE_SCHEMA_VERSION, "hints": list(hints), "entries": entries}
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def _scan_repository(
    repo: Path,
    max_files: int,
    skip_dirs: set[str] | None = None,
    topic_hints: Iterable[str] | None = None,
    use_cache: bool = True,
) -> tuple[list[FileNode], list[dict[str, Any]], list[Path], bool, list[Path]]:
    """E5 fix: previously every call re-parsed every file from scratch. Now
    keyed by (path, mtime, size) -- an unchanged file's previously-computed
    FileNode/parse-errors are reused instead of re-scanned. Known limitation
    inherent to mtime+size keying: a same-second, same-size content edit
    could theoretically be missed; not a concern for the cadence this tool
    is used at (interactive `--graph`/`--context` invocations, not a
    file-watcher)."""
    nodes: list[FileNode] = []
    parse_errors: list[dict[str, Any]] = []
    hints = tuple(topic_hints) if topic_hints is not None else resolve_topic_hints(repo)
    files, truncated, dropped = _iter_files(repo, max_files=max_files, skip_dirs=skip_dirs)

    cache_entries = _load_scan_cache(repo, hints) if use_cache else {}
    new_cache_entries: dict[str, dict[str, Any]] = {}

    for path in files:
        kind = _classify_file(path)
        if kind is None:
            continue
        try:
            rel = str(path.relative_to(repo))
            stat = path.stat()
            cached = cache_entries.get(rel)
            if (
                cached is not None
                and cached.get("kind") == kind
                and cached.get("mtime") == stat.st_mtime
                and cached.get("size") == stat.st_size
            ):
                nodes.append(FileNode(**cached["node"]))
                node_errors = cached.get("parse_errors", [])
                parse_errors.extend(node_errors)
                if use_cache:
                    new_cache_entries[rel] = cached
                continue

            if kind == "python":
                node, node_errors = _scan_python(path, repo, hints)
            else:
                node, node_errors = _scan_textual(path, repo, kind, hints), []
            nodes.append(node)
            parse_errors.extend(node_errors)
            if use_cache:
                new_cache_entries[rel] = {
                    "kind": kind,
                    "mtime": stat.st_mtime,
                    "size": stat.st_size,
                    "node": node.__dict__,
                    "parse_errors": node_errors,
                }
        except OSError:
            continue

    if use_cache:
        _save_scan_cache(repo, hints, new_cache_entries)

    return nodes, parse_errors, files, truncated, dropped


def _normalize_name(text: str) -> str:
    return re.sub(r"[-.]+", "_", text).strip().lower()


def _normalize_import_name(text: str) -> str:
    return _normalize_name(text.split("[", 1)[0].split(";", 1)[0].strip())


def _relative_or_self(path: Path, repo: Path) -> str:
    try:
        return str(path.relative_to(repo))
    except ValueError:
        return str(path)


def _module_candidates_from_path(path_text: str) -> list[str]:
    rel = str(Path(path_text))
    if not rel.endswith(".py"):
        return []
    module = rel.removesuffix(".py").replace("/", ".")
    candidates = {module}
    if module.endswith(".__init__"):
        candidates.add(module[: -len(".__init__")])
    if module.startswith("src."):
        candidates.add(module[len("src."):])
    if module.startswith("src.") and module.endswith(".__init__"):
        trimmed = module[len("src.") :]
        candidates.add(trimmed[: -len(".__init__")])
    return sorted(candidate for candidate in candidates if candidate)


def build_graph(
    repo_arg: str,
    task: str = "",
    max_files: int = 20000,
    skip_dirs: set[str] | None = None,
    topic_hints: Iterable[str] | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    repo = _repo_root(Path(repo_arg))
    graph_dir = repo / ".codex_graph"
    graph_dir.mkdir(parents=True, exist_ok=True)
    effective_hints = tuple(topic_hints) if topic_hints is not None else resolve_topic_hints(repo)
    nodes, parse_errors, files, truncated, dropped = _scan_repository(
        repo, max_files=max_files, skip_dirs=skip_dirs, topic_hints=effective_hints, use_cache=use_cache
    )
    entrypoints = _likely_entrypoints(nodes, repo)
    edges = _config_edges(nodes, repo)
    call_chain = _import_call_chains(nodes, entrypoints) or _possible_call_chain(nodes, entrypoints)
    dropped_sample = [_relative_or_self(path, repo) for path in dropped]
    graph = {
        "repo": str(repo),
        "node_count": len(nodes),
        "scan_file_count": len(files),
        "scan_truncated": truncated,
        "scan_dropped_count": len(dropped),
        "scan_dropped_sample": dropped_sample,
        "topic_hints": list(effective_hints),
        "nodes": [node.__dict__ for node in nodes],
        "entrypoints": entrypoints,
        "config_edges": edges,
        # The workspace pack has always read this key; it was never written
        # before, so its "Call chain" line was permanently "none".
        "call_chain": call_chain,
        "reverse_import_index": _reverse_import_index(nodes),
        "file_reverse_import_index": _file_reverse_import_index(nodes),
        "parse_error_count": len(parse_errors),
        "parse_errors": parse_errors,
    }
    (graph_dir / "repo_graph.json").write_text(json.dumps(graph, indent=2), encoding="utf-8")
    (graph_dir / "entrypoints.json").write_text(json.dumps(entrypoints, indent=2), encoding="utf-8")
    # Hard cap: this artifact is read by agents; it must never be able to
    # blow a context window no matter how pathological the repo is.
    (graph_dir / "config_edges.json").write_text(
        json.dumps(edges[:CONFIG_EDGES_ARTIFACT_CAP], indent=2), encoding="utf-8"
    )
    context_pack = _build_context_pack(repo, nodes, task, config_edges=edges)
    if truncated or parse_errors:
        extra: list[str] = ["", "## Graph notes"]
        if truncated:
            extra.append(
                f"- Scan truncated after `{len(files)}` candidate files "
                f"(`{len(dropped)}` lower-priority files dropped; entrypoint-like and "
                "shallower/recently-modified files were kept preferentially)."
            )
            if dropped_sample:
                extra.append("- Dropped file sample (not exhaustive):")
                extra.extend(f"  - `{item}`" for item in dropped_sample[:10])
        if parse_errors:
            extra.append(f"- Python parse issues: `{len(parse_errors)}` file(s).")
            for item in parse_errors[:10]:
                extra.append(
                    f"- `{Path(item['file']).relative_to(repo) if Path(item['file']).is_relative_to(repo) else item['file']}`"
                    f":{item.get('line') or '?'} {item['type']} {item['message']}"
                )
        context_pack = context_pack.rstrip() + "\n" + "\n".join(extra) + "\n"
    (graph_dir / "context_pack.md").write_text(context_pack, encoding="utf-8")
    return graph


def _workspace_dir(repo: Path) -> Path:
    return repo / WORKSPACE_GRAPH_DIRNAME


def _workspace_path(repo: Path, basename: str) -> Path:
    return _workspace_dir(repo) / basename


def _workspace_config_path(repo: Path) -> Path:
    return _workspace_path(repo, WORKSPACE_CONFIG_BASENAME)


def _workspace_context_pack_path(repo: Path) -> Path:
    return _workspace_path(repo, WORKSPACE_CONTEXT_PACK_BASENAME)


def _workspace_dependency_repos_path(repo: Path) -> Path:
    return _workspace_path(repo, WORKSPACE_DEPENDENCY_REPOS_BASENAME)


def _workspace_dependency_edges_path(repo: Path) -> Path:
    return _workspace_path(repo, WORKSPACE_DEPENDENCY_EDGES_BASENAME)


def _workspace_read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    if yaml is None:
        raise RuntimeError("PyYAML is required to read workspace.yaml")
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ValueError(f"workspace config must be a mapping: {path}")
    return payload


def _workspace_dump_yaml(payload: dict[str, Any]) -> str:
    if yaml is not None:
        return yaml.safe_dump(payload, sort_keys=False, default_flow_style=False)
    return json.dumps(payload, indent=2)


def _workspace_repo_aliases(repo: Path, package_name: str | None = None) -> list[str]:
    aliases: set[str] = {
        _normalize_import_name(repo.name),
        _normalize_import_name(repo.name.replace("-", "_")),
    }
    if package_name:
        aliases.add(_normalize_import_name(package_name))
    pyproject = repo / "pyproject.toml"
    if pyproject.exists():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        project = data.get("project") if isinstance(data, dict) else {}
        if isinstance(project, dict):
            project_name = project.get("name")
            if isinstance(project_name, str) and project_name.strip():
                aliases.add(_normalize_import_name(project_name))
        tool = data.get("tool") if isinstance(data, dict) else {}
        if isinstance(tool, dict):
            poetry = tool.get("poetry")
            if isinstance(poetry, dict):
                poetry_name = poetry.get("name")
                if isinstance(poetry_name, str) and poetry_name.strip():
                    aliases.add(_normalize_import_name(poetry_name))
    setup_cfg = repo / "setup.cfg"
    if setup_cfg.exists():
        parser = configparser.ConfigParser()
        parser.read(setup_cfg, encoding="utf-8")
        if parser.has_option("metadata", "name"):
            aliases.add(_normalize_import_name(parser.get("metadata", "name")))
    for parent in (repo, repo / "src"):
        if not parent.exists() or not parent.is_dir():
            continue
        for child in parent.iterdir():
            if child.is_dir() and (
                (child / "__init__.py").exists()
                or any(child.glob("*.py"))
            ):
                aliases.add(_normalize_import_name(child.name))
    return sorted(alias for alias in aliases if alias)


def _workspace_repo_identity_aliases(repo: Path) -> list[str]:
    aliases: set[str] = {
        _normalize_import_name(repo.name),
        _normalize_import_name(repo.name.replace("-", "_")),
    }
    pyproject = repo / "pyproject.toml"
    if pyproject.exists():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        project = data.get("project") if isinstance(data, dict) else {}
        if isinstance(project, dict):
            project_name = project.get("name")
            if isinstance(project_name, str) and project_name.strip():
                aliases.add(_normalize_import_name(project_name))
        tool = data.get("tool") if isinstance(data, dict) else {}
        if isinstance(tool, dict):
            poetry = tool.get("poetry")
            if isinstance(poetry, dict):
                poetry_name = poetry.get("name")
                if isinstance(poetry_name, str) and poetry_name.strip():
                    aliases.add(_normalize_import_name(poetry_name))
    setup_cfg = repo / "setup.cfg"
    if setup_cfg.exists():
        parser = configparser.ConfigParser()
        parser.read(setup_cfg, encoding="utf-8")
        if parser.has_option("metadata", "name"):
            aliases.add(_normalize_import_name(parser.get("metadata", "name")))
    return sorted(alias for alias in aliases if alias)


def _workspace_repo_matches_package(repo: Path | None, package: str) -> bool:
    if repo is None:
        return False
    normalized_package = _normalize_import_name(package)
    if not normalized_package:
        return False
    return normalized_package in {_normalize_import_name(alias) for alias in _workspace_repo_identity_aliases(repo)}


def _extract_requirement_name(requirement: str) -> str | None:
    text = requirement.split(";", 1)[0].strip()
    if not text or text.startswith("#"):
        return None
    if text.startswith("-e "):
        text = text[3:].strip()
    if text.startswith("--editable"):
        parts = text.split(None, 1)
        text = parts[1].strip() if len(parts) > 1 else ""
    if " @ " in text:
        text = text.split(" @ ", 1)[0].strip()
    match = re.match(r"([A-Za-z0-9_.-]+)", text)
    if not match:
        return None
    return _normalize_import_name(match.group(1))


def _project_dependency_hints(repo: Path) -> set[str]:
    names: set[str] = set()
    pyproject = repo / "pyproject.toml"
    if pyproject.exists():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        if isinstance(data, dict):
            project = data.get("project")
            if isinstance(project, dict):
                for requirement in project.get("dependencies", []) or []:
                    if isinstance(requirement, str):
                        name = _extract_requirement_name(requirement)
                        if name:
                            names.add(name)
                optional_dependencies = project.get("optional-dependencies", {})
                if isinstance(optional_dependencies, dict):
                    for values in optional_dependencies.values():
                        for requirement in values or []:
                            if isinstance(requirement, str):
                                name = _extract_requirement_name(requirement)
                                if name:
                                    names.add(name)
            tool = data.get("tool")
            if isinstance(tool, dict):
                poetry = tool.get("poetry")
                if isinstance(poetry, dict):
                    dependencies = poetry.get("dependencies", {})
                    if isinstance(dependencies, dict):
                        for dep_name, dep_value in dependencies.items():
                            if dep_name.lower() != "python":
                                names.add(_normalize_import_name(dep_name))
                            if isinstance(dep_value, str):
                                extracted = _extract_requirement_name(dep_value)
                                if extracted:
                                    names.add(extracted)
                            elif isinstance(dep_value, dict):
                                path = dep_value.get("path")
                                if isinstance(path, str):
                                    names.add(_normalize_import_name(Path(path).name))
    requirements = repo / "requirements.txt"
    if requirements.exists():
        for line in requirements.read_text(encoding="utf-8", errors="ignore").splitlines():
            name = _extract_requirement_name(line)
            if name:
                names.add(name)
    setup_cfg = repo / "setup.cfg"
    if setup_cfg.exists():
        parser = configparser.ConfigParser()
        parser.read(setup_cfg, encoding="utf-8")
        for section in ("options", "options.extras_require"):
            if parser.has_option(section, "install_requires"):
                for line in parser.get(section, "install_requires").splitlines():
                    name = _extract_requirement_name(line)
                    if name:
                        names.add(name)
        for section in parser.sections():
            if section.startswith("options.extras_require"):
                for value in parser[section].values():
                    for line in value.splitlines():
                        name = _extract_requirement_name(line)
                        if name:
                            names.add(name)
    setup_py = repo / "setup.py"
    if setup_py.exists():
        text = setup_py.read_text(encoding="utf-8", errors="ignore")
        for package in WORKSPACE_DETECT_PACKAGES:
            if package in text.lower():
                names.add(package)
        for line in text.splitlines():
            name = _extract_requirement_name(line)
            if name:
                names.add(name)
    return names


def _package_distribution_roots(package: str) -> tuple[Path | None, bool, list[str]]:
    resolved_root: Path | None = None
    editable = False
    origins: list[str] = []
    distribution_names = importlib_metadata.packages_distributions().get(package, [])
    if not distribution_names:
        distribution_names = [package]
    for dist_name in distribution_names:
        try:
            dist = importlib_metadata.distribution(dist_name)
        except importlib_metadata.PackageNotFoundError:
            continue
        direct_url_text = dist.read_text("direct_url.json")
        if not direct_url_text:
            continue
        try:
            direct_url = json.loads(direct_url_text)
        except json.JSONDecodeError:
            continue
        url = direct_url.get("url")
        if not isinstance(url, str) or not url.startswith("file://"):
            continue
        path = Path(urlparse(url).path).resolve()
        origins.append(str(path))
        if direct_url.get("dir_info", {}).get("editable"):
            editable = True
        candidate = _infer_repo_root_from_path(path)
        if candidate is not None:
            resolved_root = candidate
            break
        resolved_root = path
    return resolved_root, editable, origins


def _spec_origin_path(spec: Any) -> Path | None:
    if spec is None:
        return None
    origin = getattr(spec, "origin", None)
    if origin and origin != "namespace":
        try:
            return Path(origin).resolve()
        except OSError:
            return Path(origin)
    locations = getattr(spec, "submodule_search_locations", None)
    if locations:
        for location in locations:
            if location:
                try:
                    return Path(location).resolve()
                except OSError:
                    return Path(location)
    return None


def _infer_repo_root_from_path(path: Path | None) -> Path | None:
    if path is None:
        return None
    candidate = path.resolve()
    if candidate.is_file():
        candidate = candidate.parent
    for parent in [candidate, *candidate.parents]:
        if _looks_like_project_dir(parent):
            return parent.resolve()
    return None


def _workspace_coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        return default
    return bool(value)


def _workspace_is_third_party_dependency_path(path: Path | None) -> bool:
    if path is None:
        return False
    candidate = path.resolve()
    candidate_text = str(candidate).lower()
    if any(marker in candidate_text for marker in ("feedstock_root", "/site-packages/", "/dist-packages/", "/build_artifacts/")):
        return True
    for part in candidate.parts:
        lower = part.lower()
        if lower in WORKSPACE_THIRD_PARTY_PATH_MARKERS:
            return True
        if lower.endswith(WORKSPACE_THIRD_PARTY_PATH_SUFFIXES):
            return True
    return False


def _workspace_include_third_party_dependencies(spec: dict[str, Any] | None = None) -> bool:
    if spec is not None:
        value = spec.get("include_third_party_dependencies")
        if value is not None:
            return _workspace_coerce_bool(value, default=False)
    env_value = os.environ.get(WORKSPACE_INCLUDE_THIRD_PARTY_ENV)
    if env_value is None:
        env_value = os.environ.get(WORKSPACE_INCLUDE_THIRD_PARTY_ENV_LEGACY)
    return _workspace_coerce_bool(env_value, default=False)


def _find_common_sibling_repo(package: str, aliases: list[str]) -> Path | None:
    if not HOME_ROOT.exists():
        return None
    normalized_package = _normalize_import_name(package)
    if normalized_package not in WORKSPACE_DETECT_PACKAGES:
        return None
    package_variants = {
        normalized_package,
        package.lower(),
        *(_normalize_import_name(alias) for alias in aliases),
    }
    for child in HOME_ROOT.iterdir():
        if not child.is_dir():
            continue
        child_aliases = {_normalize_import_name(alias) for alias in _workspace_repo_identity_aliases(child)}
        if any(variant and variant in child_aliases for variant in package_variants):
            if _looks_like_project_dir(child):
                return child.resolve()
    return None


def _resolve_dependency_record(package: str, repo: Path) -> dict[str, Any]:
    normalized = _normalize_import_name(package)
    spec = importlib_util.find_spec(package)
    import_origin_path = _spec_origin_path(spec)
    direct_root, editable, editable_origins = _package_distribution_roots(package)
    repo_root = _infer_repo_root_from_path(import_origin_path)
    if repo_root is not None and not _workspace_repo_matches_package(repo_root, package):
        repo_root = None
    detected_by: list[str] = []
    if spec is not None:
        detected_by.append("find_spec")
    if import_origin_path is not None:
        detected_by.append("import_origin")
    if editable_origins:
        detected_by.append("editable_install")
    if direct_root is not None and direct_root != repo_root:
        if not _workspace_repo_matches_package(direct_root, package):
            direct_root = None
        else:
            repo_root = direct_root
            detected_by.append("direct_url")
    aliases = _workspace_repo_aliases(repo_root if repo_root is not None else Path(package), package_name=package)
    sibling_root = _find_common_sibling_repo(package, aliases)
    if sibling_root is not None and (repo_root is None or repo_root == direct_root):
        repo_root = sibling_root
        detected_by.append("common_sibling")
    present = bool(spec or import_origin_path or direct_root or sibling_root)
    repo_root_str = str(repo_root) if repo_root is not None else None
    import_origin = str(import_origin_path) if import_origin_path is not None else (editable_origins[0] if editable_origins else None)
    is_local_repo = repo_root_str is not None
    third_party_artifact = _workspace_is_third_party_dependency_path(repo_root) or _workspace_is_third_party_dependency_path(import_origin_path)
    return {
        "package": normalized,
        "requested_package": package,
        "import_origin": import_origin,
        "likely_repo_root": repo_root_str,
        "editable": bool(editable and is_local_repo and not third_party_artifact),
        "present": present,
        "mode": "read_only" if is_local_repo else "external",
        "third_party_artifact": third_party_artifact,
        "detected_by": sorted(set(detected_by)),
        "aliases": aliases,
        "status": "local" if is_local_repo and not third_party_artifact else ("third_party" if third_party_artifact else ("external" if present else "missing")),
    }


def _workspace_detect_dependencies(repo: Path) -> list[dict[str, Any]]:
    include_third_party = _workspace_include_third_party_dependencies()
    primary_root = repo.resolve()
    imports, _, _, _, _ = _scan_repository(repo, max_files=5000)
    import_names: set[str] = set()
    for node in imports:
        for imported in node.imports:
            root = _normalize_import_name(imported.split(".", 1)[0])
            if root and root not in sys.stdlib_module_names:
                import_names.add(root)
    hints = _project_dependency_hints(repo)
    candidates = set(WORKSPACE_DETECT_PACKAGES)
    candidates.update(import_names)
    candidates.update(hints)
    results: list[dict[str, Any]] = []
    for package in sorted(candidates):
        record = _resolve_dependency_record(package, repo)
        if record.get("likely_repo_root") and Path(str(record["likely_repo_root"])).resolve() == primary_root:
            continue
        if not include_third_party and record.get("third_party_artifact"):
            continue
        if package in WORKSPACE_DETECT_PACKAGES or record["likely_repo_root"] is not None or record["package"] in hints or package in import_names:
            results.append(record)
    results.sort(key=lambda item: item["package"])
    return results


def _workspace_normalize_dependency_entry(entry: Any) -> dict[str, Any]:
    if isinstance(entry, str):
        return {
            "name": _normalize_import_name(Path(entry).name or entry),
            "package": _normalize_import_name(Path(entry).name or entry),
            "repo_root": entry,
            "mode": "read_only",
            "editable": False,
            "present": True,
        }
    if not isinstance(entry, dict):
        raise TypeError(f"dependency entry must be a mapping or string, got {type(entry).__name__}")
    payload = dict(entry)
    repo_root = payload.get("repo_root") or payload.get("path") or payload.get("root")
    package = payload.get("package") or payload.get("name") or (Path(str(repo_root)).name if repo_root else "")
    payload["name"] = _normalize_import_name(str(payload.get("name") or package or repo_root or "dependency"))
    payload["package"] = _normalize_import_name(str(package or payload["name"]))
    payload["repo_root"] = str(repo_root) if repo_root else None
    payload["mode"] = payload.get("mode", "read_only")
    payload["editable"] = bool(payload.get("editable", False))
    payload["present"] = bool(payload.get("present", bool(repo_root)))
    payload.setdefault("detected_by", [])
    payload.setdefault("aliases", [])
    payload.setdefault("import_origin", payload.get("import_origin"))
    return payload


def _workspace_load_spec(repo: Path) -> dict[str, Any]:
    config_path = _workspace_config_path(repo)
    payload = _workspace_read_yaml(config_path)
    if not payload:
        payload = {
            "primary_repo": str(repo),
            "dependency_repos": [],
            "exclude_dirs": list(WORKSPACE_DEFAULT_EXCLUDES),
            "include_third_party_dependencies": _workspace_include_third_party_dependencies(),
            "edit_policy": {
                "primary_repo": "editable",
                "dependency_repos": "read_only_unless_explicit",
            },
        }
    payload.setdefault("primary_repo", str(repo))
    payload.setdefault("dependency_repos", [])
    payload.setdefault("exclude_dirs", list(WORKSPACE_DEFAULT_EXCLUDES))
    payload.setdefault("include_third_party_dependencies", _workspace_include_third_party_dependencies(payload))
    payload.setdefault(
        "edit_policy",
        {
            "primary_repo": "editable",
            "dependency_repos": "read_only_unless_explicit",
        },
    )
    payload.setdefault("scan_limits", {"primary": 20000, "dependency": 5000})
    dependency_entries = payload.get("dependency_repos", [])
    if isinstance(dependency_entries, list):
        payload["dependency_repos"] = [_workspace_normalize_dependency_entry(entry) for entry in dependency_entries]
    else:
        payload["dependency_repos"] = []
    exclude_dirs = payload.get("exclude_dirs", [])
    if isinstance(exclude_dirs, list):
        payload["exclude_dirs"] = sorted({str(item) for item in exclude_dirs if str(item)})
    else:
        payload["exclude_dirs"] = list(WORKSPACE_DEFAULT_EXCLUDES)
    payload["include_third_party_dependencies"] = _workspace_coerce_bool(
        payload.get("include_third_party_dependencies", False),
        default=False,
    )
    return payload


def _workspace_write_spec(repo: Path, payload: dict[str, Any]) -> Path:
    import shutil
    import time

    graph_dir = _workspace_dir(repo)
    graph_dir.mkdir(parents=True, exist_ok=True)
    config_path = _workspace_config_path(repo)
    if config_path.exists():
        backup_path = config_path.with_name(f"{config_path.name}.bak.{time.strftime('%Y%m%d_%H%M%S')}")
        shutil.copy2(config_path, backup_path)
    config_payload = dict(payload)
    config_payload.pop("external_dependencies", None)
    config_payload["primary_repo"] = str(repo)
    config_payload["dependency_repos"] = [
        _workspace_normalize_dependency_entry(entry) for entry in config_payload.get("dependency_repos", [])
    ]
    config_payload["exclude_dirs"] = sorted({str(item) for item in config_payload.get("exclude_dirs", []) if str(item)})
    config_payload["include_third_party_dependencies"] = _workspace_coerce_bool(
        config_payload.get("include_third_party_dependencies", False),
        default=False,
    )
    config_payload.setdefault(
        "edit_policy",
        {
            "primary_repo": "editable",
            "dependency_repos": "read_only_unless_explicit",
        },
    )
    config_payload.setdefault("scan_limits", {"primary": 20000, "dependency": 5000})
    config_path.write_text(_workspace_dump_yaml(config_payload), encoding="utf-8")
    return config_path


def _workspace_dependency_repo_entries(spec: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen_roots: set[str] = set()
    include_third_party = _workspace_include_third_party_dependencies(spec)
    for entry in spec.get("dependency_repos", []):
        normalized = _workspace_normalize_dependency_entry(entry)
        repo_root = normalized.get("repo_root")
        if not repo_root:
            continue
        root_key = str(Path(repo_root).resolve())
        if not include_third_party and _workspace_is_third_party_dependency_path(Path(root_key)):
            continue
        if root_key in seen_roots:
            continue
        seen_roots.add(root_key)
        normalized["repo_root"] = root_key
        entries.append(normalized)
    return entries


def _workspace_relevance_score(node: FileNode) -> int:
    score = 0
    lower = node.path.lower()
    name = Path(node.path).name.lower()
    if ENTRYPOINT_RE.search(name):
        score += 3
    for token in (
        "train",
        "training",
        "eval",
        "launch",
        "collect",
        "policy",
        "controller",
        "wrapper",
        "adapter",
        "config",
        "openpi",
        "openvla",
        "deepreach",
        "robosuite",
        "runtime",
        "runner",
        "action",
        "scale",
    ):
        if token in lower:
            score += 2
    if node.kind in {"yaml", "json", "toml"}:
        score += 1
    if node.kind == "python" and node.functions:
        score += 1
    if node.tags:
        score += min(len(node.tags), 3)
    return score


def _workspace_summary_lines(
    repo: Path,
    nodes: list[FileNode],
    files: list[Path],
    parse_errors: list[dict[str, Any]],
    truncated: bool,
    role: str,
    package: str | None = None,
    mode: str = "read_only",
    editable: bool = False,
    import_origin: str | None = None,
) -> dict[str, Any]:
    entrypoints = _likely_entrypoints(nodes, repo)
    configs = _config_edges(nodes, repo)
    path_refs = sorted({ref for node in nodes for ref in node.path_refs})
    relevant = sorted(
        (
            {"path": node.path, "score": _workspace_relevance_score(node), "tags": node.tags}
            for node in nodes
        ),
        key=lambda item: (-int(item["score"]), item["path"]),
    )[:20]
    tags = sorted({tag for node in nodes for tag in node.tags})
    call_chain = _possible_call_chain(nodes, entrypoints)
    summary = {
        "repo": str(repo),
        "name": repo.name,
        "role": role,
        "package": package or _normalize_import_name(repo.name),
        "mode": mode,
        "editable": editable,
        "present": True,
        "import_origin": import_origin,
        "aliases": _workspace_repo_aliases(repo, package_name=package),
        "node_count": len(nodes),
        "scan_file_count": len(files),
        "scan_truncated": truncated,
        "parse_error_count": len(parse_errors),
        "parse_issues": parse_errors,
        "entrypoints": entrypoints[:12],
        "config_edges": configs[:20],
        "relevant_files": relevant,
        "call_chain": call_chain,
        "path_refs": path_refs[:40],
        "tags": tags,
    }
    return summary


def _workspace_repo_alias_map(
    primary_repo: Path,
    primary_package: str | None,
    dependency_entries: list[dict[str, Any]],
) -> dict[str, list[str]]:
    alias_map: dict[str, list[str]] = {
        str(primary_repo.resolve()): _workspace_repo_aliases(primary_repo, package_name=primary_package),
    }
    for entry in dependency_entries:
        repo_root = entry.get("repo_root")
        if not repo_root:
            continue
        root = str(Path(repo_root).resolve())
        alias_map[root] = _workspace_repo_aliases(Path(root), package_name=entry.get("package"))
    return alias_map


def _workspace_resolve_target_repo(
    ref: str,
    source_repo: Path,
    repo_alias_map: dict[str, list[str]],
    treat_as_import: bool = False,
) -> tuple[str | None, str | None]:
    source_repo = source_repo.resolve()
    raw = ref.strip()
    if not raw:
        return None, None
    normalized = _normalize_import_name(raw)
    for repo_root, aliases in repo_alias_map.items():
        if treat_as_import:
            if any(
                alias
                and (
                    alias == normalized
                    or normalized.startswith(alias + ".")
                    or normalized.startswith(alias + "_")
                )
                for alias in aliases
            ):
                return repo_root, raw
        elif any(alias and (alias == normalized or alias in normalized or normalized in alias) for alias in aliases):
            return repo_root, raw
    if treat_as_import:
        return None, None
    candidates: list[Path] = []
    path_like = Path(raw)
    if path_like.is_absolute():
        candidates.append(path_like)
    else:
        candidates.append((source_repo / path_like).resolve())
        candidates.append((source_repo.parent / path_like).resolve())
        candidates.append((HOME_ROOT / path_like).resolve())
    for candidate in candidates:
        for repo_root in repo_alias_map:
            root_path = Path(repo_root).resolve()
            if candidate == root_path or root_path in candidate.parents:
                return repo_root, str(candidate)
    return None, None


def _workspace_source_edge_kind(source_path: str) -> str:
    lower = source_path.lower()
    if "controller" in lower:
        return "controller_call"
    if "train" in lower or "training" in lower:
        return "training_call"
    if "eval" in lower or "monitor" in lower:
        return "eval_call"
    if "action" in lower and ("scale" in lower or "adapter" in lower or "codec" in lower):
        return "action_scaling_path"
    if any(token in lower for token in ("policy", "wrapper", "adapter")):
        return "policy_wrapper"
    if any(token in lower for token in ("collect", "rollout", "runtime", "runner")):
        return "runtime_call"
    return "import"


def _workspace_ref_kind(ref: str) -> str:
    lower = ref.lower()
    if lower.endswith((".yaml", ".yml", ".json", ".toml")):
        return "config_reference"
    if any(token in lower for token in ("checkpoint", "ckpt")) or lower.endswith((".pth", ".pt", ".safetensors", ".bin")):
        return "checkpoint_path"
    if "dataset" in lower or "/data" in lower or "/datasets" in lower:
        return "dataset_path"
    if "log" in lower or "/logs" in lower:
        return "log_path"
    return "path_reference"


def _workspace_collect_edges(
    repo_scans: list[dict[str, Any]],
    repo_alias_map: dict[str, list[str]],
) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str, str]] = set()
    for scan in repo_scans:
        source_repo = Path(scan["repo"])
        source_kind = _workspace_source_edge_kind(scan.get("primary_path_hint", scan.get("name", "")) or scan.get("package", ""))
        nodes = scan.get("nodes", [])
        for node in nodes:
            node_path = node.path
            source_kind = _workspace_source_edge_kind(node_path)
            for imported in node.imports:
                target_repo, target_ref = _workspace_resolve_target_repo(imported, source_repo, repo_alias_map, treat_as_import=True)
                if not target_repo or target_repo == str(source_repo.resolve()):
                    continue
                edge = {
                    "kind": source_kind if source_kind != "import" else "import",
                    "source_repo": str(source_repo),
                    "source_path": node_path,
                    "target_repo": target_repo,
                    "target_ref": target_ref,
                    "import_name": imported,
                    "evidence": "ast_import",
                }
                key = (
                    edge["kind"],
                    edge["source_repo"],
                    edge["source_path"],
                    edge["target_repo"],
                    edge["target_ref"],
                    edge["import_name"],
                )
                if key not in seen:
                    seen.add(key)
                    edges.append(edge)
            for ref in node.path_refs:
                target_repo, target_ref = _workspace_resolve_target_repo(ref, source_repo, repo_alias_map)
                if not target_repo or target_repo == str(source_repo.resolve()):
                    continue
                edge = {
                    "kind": _workspace_ref_kind(ref),
                    "source_repo": str(source_repo),
                    "source_path": node_path,
                    "target_repo": target_repo,
                    "target_ref": target_ref,
                    "evidence": "path_reference",
                }
                key = (
                    edge["kind"],
                    edge["source_repo"],
                    edge["source_path"],
                    edge["target_repo"],
                    edge["target_ref"],
                    ref,
                )
                if key not in seen:
                    seen.add(key)
                    edges.append(edge)
    return edges


def _workspace_edge_counts(edges: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for edge in edges:
        counts[str(edge["kind"])] += 1
    return dict(sorted(counts.items()))


def _workspace_primary_summary_from_graph(graph: dict[str, Any]) -> dict[str, Any]:
    nodes = [FileNode(**node) for node in graph.get("nodes", [])]
    files = [Path(node["path"]) for node in graph.get("nodes", [])]
    summary = _workspace_summary_lines(
        repo=Path(graph["repo"]),
        nodes=nodes,
        files=files,
        parse_errors=graph.get("parse_errors", []),
        truncated=bool(graph.get("scan_truncated", False)),
        role="primary",
        package=_normalize_import_name(Path(graph["repo"]).name),
        mode="editable",
        editable=True,
        import_origin=graph.get("repo"),
    )
    return summary


def _workspace_dependency_scan(
    repo_root: Path,
    package: str,
    mode: str,
    editable: bool,
    import_origin: str | None,
    max_files: int,
    skip_dirs: set[str],
) -> tuple[dict[str, Any], list[FileNode]]:
    nodes, parse_errors, files, truncated, _dropped = _scan_repository(repo_root, max_files=max_files, skip_dirs=skip_dirs)
    summary = _workspace_summary_lines(
        repo=repo_root,
        nodes=nodes,
        files=files,
        parse_errors=parse_errors,
        truncated=truncated,
        role="dependency",
        package=package,
        mode=mode,
        editable=editable,
        import_origin=import_origin,
    )
    return summary, nodes


def _workspace_config_for_detection(repo: Path) -> dict[str, Any]:
    detected = _workspace_detect_dependencies(repo)
    dependency_repos: list[dict[str, Any]] = []
    seen_roots: set[str] = set()
    external_dependencies: list[dict[str, Any]] = []
    for record in detected:
        repo_root = record.get("likely_repo_root")
        if repo_root:
            root = str(Path(repo_root).resolve())
            if root in seen_roots:
                continue
            seen_roots.add(root)
            dependency_repos.append(
                {
                    "name": record["package"],
                    "package": record["package"],
                    "repo_root": root,
                    "mode": "read_only",
                    "editable": bool(record.get("editable", False)),
                    "present": bool(record.get("present", False)),
                    "import_origin": record.get("import_origin"),
                    "detected_by": record.get("detected_by", []),
                    "aliases": record.get("aliases", []),
                }
            )
        elif record["package"] in WORKSPACE_DETECT_PACKAGES:
            external_dependencies.append(record)
    return {
        "primary_repo": str(repo),
        "dependency_repos": dependency_repos,
        "external_dependencies": external_dependencies,
        "exclude_dirs": list(WORKSPACE_DEFAULT_EXCLUDES),
        "edit_policy": {
            "primary_repo": "editable",
            "dependency_repos": "read_only_unless_explicit",
        },
        "scan_limits": {"primary": 20000, "dependency": 5000},
    }


def _workspace_file_freshness(path: Path) -> str:
    if not path.exists():
        return "missing"
    import time

    seconds = max(0, int(time.time()) - int(path.stat().st_mtime))
    if seconds < 60:
        return f"fresh ({seconds}s old)"
    if seconds < 3600:
        return f"fresh ({seconds // 60}m old)"
    if seconds < 86400:
        return f"fresh ({seconds // 3600}h old)"
    return f"stale ({seconds // 86400}d old)"


def _workspace_path_summary_items(paths: list[str], limit: int = 10) -> str:
    if not paths:
        return "none"
    return ", ".join(f"`{item}`" for item in paths[:limit])


def _workspace_confirm_write(prompt: str, yes: bool) -> bool:
    if yes:
        return True
    if not sys.stdin.isatty():
        raise RuntimeError(prompt)
    reply = input(f"{prompt} [y/N] ").strip().lower()
    return reply in {"y", "yes"}


def _workspace_context_pack_text(
    repo: Path,
    workspace: dict[str, Any],
    primary_graph: dict[str, Any],
    primary_nodes: list[FileNode],
    dependency_summaries: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    external_dependencies: list[dict[str, Any]],
) -> str:
    primary_entrypoints = primary_graph.get("entrypoints", [])[:8]
    primary_configs = primary_graph.get("config_edges", [])[:12]
    primary_relevant = sorted(
        (
            {"path": node.path, "score": _workspace_relevance_score(node), "tags": node.tags}
            for node in primary_nodes
        ),
        key=lambda item: (-int(item["score"]), item["path"]),
    )[:12]
    lines: list[str] = [
        f"# Workspace Context Pack for {repo.name}",
        "",
        "## Workspace summary",
        f"- Primary repo: `{workspace['primary_repo']}`",
        f"- Workspace config: `{_workspace_config_path(repo)}`",
        f"- Workspace graph: `{_workspace_context_pack_path(repo)}`",
        f"- Dependency edge file: `{_workspace_dependency_edges_path(repo)}`",
        f"- Dependency repo file: `{_workspace_dependency_repos_path(repo)}`",
        "- Edit policy: primary repo editable; dependency repos read-only unless explicit task instruction says otherwise.",
        f"- Third-party dependency roots: {'included' if _workspace_include_third_party_dependencies(workspace) else f'excluded by default via `{WORKSPACE_INCLUDE_THIRD_PARTY_ENV}`'}",
        f"- Excluded dirs: {', '.join(workspace.get('exclude_dirs', list(WORKSPACE_DEFAULT_EXCLUDES)))}",
        "",
        "## Primary repo summary",
        f"- Root: `{workspace['primary_repo']}`",
        f"- Nodes scanned: `{primary_graph.get('node_count', len(primary_nodes))}`",
        f"- Parse issues: `{primary_graph.get('parse_error_count', 0)}`",
        f"- Entry points: {_workspace_path_summary_items([item['path'] for item in primary_entrypoints])}",
        f"- Config refs: {_workspace_path_summary_items(list(dict.fromkeys(edge['source'] for edge in primary_configs)))}",
        f"- Relevant files: {_workspace_path_summary_items([item['path'] for item in primary_relevant])}",
        f"- Call chain: {' | '.join(primary_graph.get('call_chain', [])) or 'none'}",
        f"- Single-repo context pack: `{_workspace_dir(repo) / 'context_pack.md'}`",
        "",
        "## Dependency repo summaries",
    ]
    if dependency_summaries:
        for summary in dependency_summaries:
            lines.extend(
                [
                    f"### {summary['name']}",
                    f"- Root: `{summary['repo']}`",
                    f"- Package: `{summary.get('package') or summary['name']}`",
                    f"- Mode: `{summary.get('mode', 'read_only')}`",
                    f"- Editable: `{summary.get('editable', False)}`",
                    f"- Import origin: `{summary.get('import_origin') or 'unknown'}`",
                    f"- Nodes scanned: `{summary.get('node_count', 0)}`",
                    f"- Parse issues: `{summary.get('parse_error_count', 0)}`",
                    f"- Entry points: {_workspace_path_summary_items([item['path'] for item in summary.get('entrypoints', [])])}",
                    f"- Config refs: {_workspace_path_summary_items([edge['source'] for edge in summary.get('config_edges', [])])}",
                    f"- Relevant files: {_workspace_path_summary_items([item['path'] for item in summary.get('relevant_files', [])])}",
                    f"- Call chain: {' | '.join(summary.get('call_chain', [])) or 'none'}",
                ]
            )
            parse_issues = summary.get("parse_issues", [])
            if parse_issues:
                lines.append("- Parse issues:")
                for item in parse_issues[:5]:
                    rel = item.get("file") or item.get("path") or "unknown"
                    lines.append(
                        f"  - `{rel}`:{item.get('line') or '?'} {item.get('type') or 'ParseError'} {item.get('message') or ''}".rstrip()
                    )
    else:
        lines.append("- No dependency repos detected or configured.")

    if external_dependencies:
        lines.extend(["", "## External dependency notes"])
        for item in external_dependencies[:10]:
            lines.append(
                f"- `{item['package']}` origin `{item.get('import_origin') or 'unknown'}` root `{item.get('likely_repo_root') or 'external'}`"
            )

    lines.extend(["", "## Cross-repo edges"])
    if edges:
        for edge in edges[:60]:
            source = f"`{edge['source_repo']}`::{edge['source_path']}"
            target = f"`{edge['target_repo']}`::{edge['target_ref']}"
            lines.append(f"- `{edge['kind']}` {source} -> {target}")
        if len(edges) > 60:
            lines.append(f"- ... {len(edges) - 60} more edges in {WORKSPACE_DEPENDENCY_EDGES_BASENAME}")
    else:
        lines.append("- No cross-repo edges detected.")

    lines.extend(
        [
            "",
            "## Likely active eval/training path",
            f"- {primary_entrypoints[0]['path'] if primary_entrypoints else 'No obvious entrypoint found.'}",
            "",
            "## Config-to-code path",
            f"- {primary_configs[0]['source'] if primary_configs else 'No cross-repo config edge found.'}",
            "",
            "## Policy wrapper path",
            f"- {next((edge['source_path'] for edge in edges if edge['kind'] == 'policy_wrapper'), 'No cross-repo policy wrapper edge found.')}",
            "",
            "## Controller/action scaling path",
            f"- {next((edge['source_path'] for edge in edges if edge['kind'] in {'controller_call', 'action_scaling_path'}), 'No cross-repo controller/action scaling edge found.')}",
            "",
            "## Reachability/DeepReach path",
            f"- {next((summary['repo'] for summary in dependency_summaries if 'deepreach' in summary['name'].lower() or 'deepreach' in summary.get('package', '').lower()), 'No DeepReach repo detected.')}",
            "",
            "## Checkpoint/data/log path references",
        ]
    )
    artifact_edges = [edge for edge in edges if edge["kind"] in {"checkpoint_path", "dataset_path", "log_path"}]
    if artifact_edges:
        for edge in artifact_edges[:20]:
            lines.append(f"- `{edge['kind']}` {edge['source_path']} -> {edge['target_ref']}")
    else:
        lines.append("- No cross-repo checkpoint/data/log references found.")

    lines.extend(
        [
            "",
            "## Edit policy",
            "- Editable: primary repo only.",
            "- Read-only: dependency repos unless the task explicitly says to edit them.",
            "- If dependency code is implicated, prefer config, wrapper, adapter, or version/path fixes before editing the dependency repo itself.",
            "- Never silently edit OpenPI, DeepReach, robosuite, MuJoCo, or external library repos.",
            "",
            "## Suggested smoke tests",
            "- `cdx-agent --detect-deps`",
            "- `cdx-agent --workspace-doctor`",
            "- a short rollout or wrapper smoke for the active policy/controller path",
            "- validate YAML/JSON/TOML parsing for touched configs",
        ]
    )
    return "\n".join(lines) + "\n"


def _workspace_build_primary_and_dependencies(
    repo: Path,
    workspace: dict[str, Any],
) -> tuple[dict[str, Any], list[FileNode], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], str]:
    primary_max = int(workspace.get("scan_limits", {}).get("primary", 20000))
    dependency_max = int(workspace.get("scan_limits", {}).get("dependency", 5000))
    exclude_dirs = set(str(item) for item in workspace.get("exclude_dirs", []))
    primary_graph = build_graph(str(repo), max_files=primary_max, skip_dirs=exclude_dirs)
    primary_nodes = [FileNode(**node) for node in primary_graph.get("nodes", [])]
    primary_summary = _workspace_summary_lines(
        repo=repo,
        nodes=primary_nodes,
        files=[repo / node.path for node in primary_nodes],
        parse_errors=primary_graph.get("parse_errors", []),
        truncated=bool(primary_graph.get("scan_truncated", False)),
        role="primary",
        package=_normalize_import_name(repo.name),
        mode="editable",
        editable=True,
        import_origin=str(repo),
    )

    dependency_summaries: list[dict[str, Any]] = []
    repo_scans: list[dict[str, Any]] = [
        {
            "repo": str(repo),
            "name": repo.name,
            "package": primary_summary.get("package"),
            "mode": "editable",
            "editable": True,
            "nodes": primary_nodes,
            "primary_path_hint": primary_summary.get("package"),
            "role": "primary",
        }
    ]
    for entry in _workspace_dependency_repo_entries(workspace):
        repo_root = Path(entry["repo_root"])
        summary, nodes = _workspace_dependency_scan(
            repo_root=repo_root,
            package=entry.get("package") or entry["name"],
            mode=entry.get("mode", "read_only"),
            editable=bool(entry.get("editable", False)),
            import_origin=entry.get("import_origin"),
            max_files=dependency_max,
            skip_dirs=exclude_dirs,
        )
        summary["detected_by"] = entry.get("detected_by", [])
        summary["aliases"] = entry.get("aliases", [])
        dependency_summaries.append(summary)
        repo_scans.append(
            {
                "repo": str(repo_root),
                "name": summary["name"],
                "package": summary.get("package"),
                "mode": summary.get("mode", "read_only"),
                "editable": summary.get("editable", False),
                "nodes": nodes,
                "primary_path_hint": summary.get("package"),
                "role": "dependency",
            }
        )

    repo_alias_map = _workspace_repo_alias_map(repo, primary_summary.get("package"), workspace.get("dependency_repos", []))
    edges = _workspace_collect_edges(repo_scans, repo_alias_map)
    external_dependencies = workspace.get("external_dependencies", [])
    workspace_context_pack = _workspace_context_pack_text(
        repo=repo,
        workspace=workspace,
        primary_graph=primary_graph,
        primary_nodes=primary_nodes,
        dependency_summaries=dependency_summaries,
        edges=edges,
        external_dependencies=external_dependencies,
    )
    workspace_payload = {
        "workspace": workspace,
        "primary_repo": primary_summary,
        "dependency_repos": dependency_summaries,
        "external_dependencies": external_dependencies,
        "edges": edges,
        "edge_counts": _workspace_edge_counts(edges),
    }
    return workspace_payload, primary_graph, primary_nodes, dependency_summaries, edges, workspace_context_pack


def _workspace_write_outputs(repo: Path, payload: dict[str, Any], context_pack: str) -> dict[str, str]:
    graph_dir = _workspace_dir(repo)
    graph_dir.mkdir(parents=True, exist_ok=True)
    dependency_repos_path = _workspace_dependency_repos_path(repo)
    dependency_edges_path = _workspace_dependency_edges_path(repo)
    context_path = _workspace_context_pack_path(repo)
    dependency_repos_path.write_text(json.dumps({
        "primary_repo": payload["primary_repo"],
        "dependency_repos": payload["dependency_repos"],
        "external_dependencies": payload.get("external_dependencies", []),
    }, indent=2), encoding="utf-8")
    dependency_edges_path.write_text(json.dumps({
        "workspace": payload["workspace"],
        "edges": payload["edges"],
        "edge_counts": payload["edge_counts"],
    }, indent=2), encoding="utf-8")
    context_path.write_text(context_pack, encoding="utf-8")
    return {
        "workspace_context_pack": str(context_path),
        "dependency_repos": str(dependency_repos_path),
        "dependency_edges": str(dependency_edges_path),
    }


def command_detect_deps(args: argparse.Namespace) -> int:
    repo = resolve_repo_root(args.repo, force_home_scan=bool(getattr(args, "force_home_scan", False)))
    detected = _workspace_detect_dependencies(repo)
    print(json.dumps(detected, indent=2))
    return 0


def command_init_workspace(args: argparse.Namespace) -> int:
    repo = resolve_repo_root(args.repo, force_home_scan=bool(getattr(args, "force_home_scan", False)))
    prompt = f"Create workspace.yaml in {repo / WORKSPACE_GRAPH_DIRNAME}?"
    if not _workspace_confirm_write(prompt, bool(getattr(args, "yes", False))):
        raise SystemExit(1)
    workspace = _workspace_config_for_detection(repo)
    config_path = _workspace_write_spec(repo, workspace)
    print(
        json.dumps(
            {
                "workspace_config": str(config_path),
                "primary_repo": workspace["primary_repo"],
                "dependency_count": len(workspace["dependency_repos"]),
                "external_dependency_count": len(workspace.get("external_dependencies", [])),
            },
            indent=2,
        )
    )
    return 0


def command_workspace_graph(args: argparse.Namespace) -> int:
    repo = resolve_repo_root(args.repo, force_home_scan=bool(getattr(args, "force_home_scan", False)))
    workspace = _workspace_load_spec(repo)
    if not workspace.get("dependency_repos") and not workspace.get("external_dependencies"):
        workspace = _workspace_config_for_detection(repo)
    payload, primary_graph, primary_nodes, dependency_summaries, edges, context_pack = _workspace_build_primary_and_dependencies(
        repo=repo,
        workspace=workspace,
    )
    paths = _workspace_write_outputs(repo, payload, context_pack)
    print(
        json.dumps(
            {
                "primary_repo": payload["primary_repo"]["repo"],
                "dependency_count": len(payload["dependency_repos"]),
                "cross_repo_edge_count": len(payload["edges"]),
                "workspace_context_pack": paths["workspace_context_pack"],
                "dependency_repos": paths["dependency_repos"],
                "dependency_edges": paths["dependency_edges"],
                "primary_node_count": primary_graph.get("node_count", len(primary_nodes)),
                "dependency_summaries": [
                    {
                        "name": summary["name"],
                        "repo": summary["repo"],
                        "package": summary.get("package"),
                        "mode": summary.get("mode"),
                        "editable": summary.get("editable"),
                        "parse_error_count": summary.get("parse_error_count", 0),
                    }
                    for summary in dependency_summaries
                ],
            },
            indent=2,
        )
    )
    return 0


def command_workspace_doctor(args: argparse.Namespace) -> int:
    repo = resolve_repo_root(args.repo, force_home_scan=bool(getattr(args, "force_home_scan", False)))
    workspace = _workspace_load_spec(repo)
    config_path = _workspace_config_path(repo)
    dependency_repos_path = _workspace_dependency_repos_path(repo)
    dependency_edges_path = _workspace_dependency_edges_path(repo)
    context_path = _workspace_context_pack_path(repo)
    detected = _workspace_detect_dependencies(repo) if not workspace.get("dependency_repos") else []
    dependency_entries = workspace.get("dependency_repos", [])
    print(f"primary_repo={workspace['primary_repo']}")
    print(f"workspace_config={config_path} {_workspace_file_freshness(config_path)}")
    print(f"workspace_context_pack={context_path} {_workspace_file_freshness(context_path)}")
    print(f"dependency_repos_json={dependency_repos_path} {_workspace_file_freshness(dependency_repos_path)}")
    print(f"dependency_edges_json={dependency_edges_path} {_workspace_file_freshness(dependency_edges_path)}")
    print("editable_scope=primary_repo_only")
    print("dependency_scope=read_only_unless_explicit")
    print(
        "third_party_dependency_scope="
        + ("included" if _workspace_include_third_party_dependencies(workspace) else f"excluded_by_default ({WORKSPACE_INCLUDE_THIRD_PARTY_ENV})")
    )
    print("dependency_repos:")
    if dependency_entries:
        for entry in dependency_entries:
            normalized = _workspace_normalize_dependency_entry(entry)
            print(
                f"- {normalized['name']} root={normalized.get('repo_root') or 'missing'} mode={normalized.get('mode', 'read_only')} editable={normalized.get('editable', False)} origin={normalized.get('import_origin') or 'unknown'}"
            )
    else:
        for record in detected:
            print(
                f"- {record['package']} root={record.get('likely_repo_root') or 'missing'} mode={record.get('mode', 'external')} editable={record.get('editable', False)} origin={record.get('import_origin') or 'unknown'}"
            )
    missing = [
        entry.get("repo_root")
        for entry in dependency_entries
        if entry.get("repo_root") and not Path(str(entry["repo_root"])).exists()
    ]
    if missing:
        print("missing_dependencies:")
        for item in missing:
            print(f"- {item}")
    else:
        print("missing_dependencies=none")
    print("import_origins:")
    origins = dependency_entries or detected
    for item in origins:
        normalized = _workspace_normalize_dependency_entry(item) if isinstance(item, dict) else item
        print(f"- {normalized.get('package') or normalized.get('name')} -> {normalized.get('import_origin') or 'unknown'}")
    print("risky_paths_excluded:")
    print(f"- {', '.join(workspace.get('exclude_dirs', list(WORKSPACE_DEFAULT_EXCLUDES)))}")
    return 0


def command_build(args: argparse.Namespace) -> int:
    repo = resolve_repo_root(args.repo, force_home_scan=bool(getattr(args, "force_home_scan", False)))
    stale = pack_staleness(repo)
    if stale:
        print(f"note: previous {stale}", file=sys.stderr)
    graph = build_graph(str(repo), task=args.task or "", max_files=args.max_files)
    payload = {
        "repo": graph["repo"],
        "node_count": graph["node_count"],
        "scan_file_count": graph["scan_file_count"],
        "scan_truncated": graph["scan_truncated"],
        "parse_error_count": graph["parse_error_count"],
    }
    if graph["parse_errors"]:
        payload["parse_errors_preview"] = graph["parse_errors"][:10]
    print(json.dumps(payload, indent=2))
    return 0


def command_context(args: argparse.Namespace) -> int:
    repo = resolve_repo_root(args.repo, force_home_scan=bool(getattr(args, "force_home_scan", False)))
    graph = build_graph(str(repo), task=args.task, max_files=args.max_files)
    repo = Path(graph["repo"])
    print((repo / ".codex_graph" / "context_pack.md").read_text(encoding="utf-8"))
    return 0


def command_relevant(args: argparse.Namespace) -> int:
    repo = resolve_repo_root(args.repo, force_home_scan=bool(getattr(args, "force_home_scan", False)))
    graph_path = repo / ".codex_graph" / "repo_graph.json"
    if not graph_path.exists():
        build_graph(str(repo), task=args.task)
    payload = json.loads(graph_path.read_text(encoding="utf-8"))
    nodes = [FileNode(**node) for node in payload["nodes"]]
    tokens = _task_tokens(args.task)
    doc_freqs = _document_frequencies(nodes, tokens)
    scored = sorted(
        (
            {"path": node.path, "score": _score_node_for_task(node, tokens, doc_freqs, len(nodes)), "tags": node.tags}
            for node in nodes
        ),
        key=lambda item: (-item["score"], item["path"]),
    )
    print(json.dumps([item for item in scored if item["score"] > 0][:20], indent=2))
    return 0


def command_impact(args: argparse.Namespace) -> int:
    repo = resolve_repo_root(args.repo, force_home_scan=bool(getattr(args, "force_home_scan", False)))
    graph_path = repo / ".codex_graph" / "repo_graph.json"
    if not graph_path.exists():
        build_graph(str(repo))
    payload = json.loads(graph_path.read_text(encoding="utf-8"))
    # E3 fix: the file-path-keyed index (only resolved, real-file edges) is
    # the precise answer and is tried first; the bare-module-name index is
    # kept only as a fallback for anything the resolver couldn't place (e.g.
    # dynamic imports), so a genuine hit doesn't quietly disappear.
    file_reverse = payload.get("file_reverse_import_index", {})
    reverse = payload.get("reverse_import_index", {})
    config_edges = payload.get("config_edges", [])
    entry_paths = {item["path"] for item in payload.get("entrypoints", [])}
    max_depth = int(getattr(args, "depth", 3) or 3)
    out: dict[str, Any] = {}
    for file_name in args.files:
        rel = str(Path(file_name))
        direct: list[str] = list(file_reverse.get(rel, []))
        if not direct:
            for module in _module_candidates_from_path(rel):
                direct.extend(reverse.get(module, []))
                if "." in module:
                    direct.extend(reverse.get(module.rsplit(".", 1)[0], []))
        direct = sorted(set(direct))
        # Transitive reverse-import closure: the blast radius of an edit is
        # everything that (indirectly) imports the changed file, not just
        # the first ring.
        depths: dict[str, int] = {path: 1 for path in direct}
        frontier = list(direct)
        depth = 1
        while frontier and depth < max_depth:
            depth += 1
            next_frontier: list[str] = []
            for current in frontier:
                for importer in file_reverse.get(current, []):
                    if importer != rel and importer not in depths:
                        depths[importer] = depth
                        next_frontier.append(importer)
            frontier = next_frontier
        transitive = [
            {"path": path, "depth": d, "entrypoint": path in entry_paths}
            for path, d in sorted(depths.items(), key=lambda kv: (kv[1], kv[0] not in entry_paths, kv[0]))
        ]
        config_refs = sorted({edge["source"] for edge in config_edges if edge.get("target") == rel})
        out[rel] = {
            "direct": direct,
            "transitive": transitive,
            "config_references": config_refs,
        }
    print(json.dumps(out, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cdx-agent graph")
    subparsers = parser.add_subparsers(dest="command", required=True)

    parser_build = subparsers.add_parser("build")
    parser_build.add_argument("--repo", required=True)
    parser_build.add_argument("--task", default="")
    parser_build.add_argument("--max-files", type=int, default=20000)
    parser_build.set_defaults(func=command_build)

    parser_context = subparsers.add_parser("context")
    parser_context.add_argument("--repo", required=True)
    parser_context.add_argument("--task", required=True)
    parser_context.add_argument("--max-files", type=int, default=20000)
    parser_context.set_defaults(func=command_context)

    parser_relevant = subparsers.add_parser("relevant")
    parser_relevant.add_argument("--repo", required=True)
    parser_relevant.add_argument("--task", required=True)
    parser_relevant.set_defaults(func=command_relevant)

    parser_impact = subparsers.add_parser("impact")
    parser_impact.add_argument("--repo", required=True)
    parser_impact.add_argument("--files", nargs="+", required=True)
    parser_impact.add_argument("--depth", type=int, default=3)
    parser_impact.set_defaults(func=command_impact)

    parser_detect_deps = subparsers.add_parser("detect-deps")
    parser_detect_deps.add_argument("--repo", required=True)
    parser_detect_deps.set_defaults(func=command_detect_deps)

    parser_init_workspace = subparsers.add_parser("init-workspace")
    parser_init_workspace.add_argument("--repo", required=True)
    parser_init_workspace.add_argument("--yes", action="store_true")
    parser_init_workspace.set_defaults(func=command_init_workspace)

    parser_workspace_graph = subparsers.add_parser("workspace-graph")
    parser_workspace_graph.add_argument("--repo", required=True)
    parser_workspace_graph.set_defaults(func=command_workspace_graph)

    parser_workspace_doctor = subparsers.add_parser("workspace-doctor")
    parser_workspace_doctor.add_argument("--repo", required=True)
    parser_workspace_doctor.set_defaults(func=command_workspace_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
