"""Configuration and path resolution for cdx_agent.

Every filesystem location cdx_agent touches is derived here, from (highest to
lowest precedence): an explicit path, the ``CDX_AGENT_CONFIG`` environment
variable, ``$XDG_CONFIG_HOME/cdx-agent/config.yaml`` (default
``~/.config/cdx-agent/config.yaml``), or defaults computed purely from
``$USER``/``$HOME``. No path is ever a hardcoded literal for a specific
account, which is what let the bash predecessor only work for one user.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import socket
import string
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

DEFAULT_ACCESS_MODE = "safe"
DEFAULT_STALE_RETENTION_DAYS = 14
ENV_CONFIG_PATH = "CDX_AGENT_CONFIG"

_PROJECT_MARKERS = (
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "package.json",
    "Cargo.toml",
    "AGENTS.md",
)
_SLUG_INVALID_RE = re.compile(r"[^a-z0-9._-]")
_NAME_INVALID_RE = re.compile(r"[^A-Za-z0-9._-]")


def xdg_config_home() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return Path(xdg) if xdg else Path.home() / ".config"


def default_config_path() -> Path:
    return xdg_config_home() / "cdx-agent" / "config.yaml"


@dataclass(frozen=True)
class Config:
    user_root: Path
    account_home: Path
    tools_root: Path
    runtime_root: Path
    workspace_manifest_root: Path
    workspace_mirror_root: Path
    skill_roots: tuple[Path, ...]
    quarantine_root: Path
    stale_retention_days: int = DEFAULT_STALE_RETENTION_DAYS
    default_access_mode: str = DEFAULT_ACCESS_MODE
    engines: dict[str, bool] = field(default_factory=lambda: {"codex": True, "claude": True})
    source_path: Path | None = None

    @classmethod
    def defaults(cls, home: Path | None = None) -> "Config":
        home = (home or Path.home()).expanduser()
        tools_root = home / "codex_tools"
        return cls(
            user_root=home,
            account_home=home,
            tools_root=tools_root,
            runtime_root=home / "codex_runtime",
            workspace_manifest_root=home / ".cdx" / "workspaces",
            workspace_mirror_root=home / ".cdx" / "dg_workspaces",
            skill_roots=(
                tools_root / "skills",
                tools_root / "skills_approved",
                tools_root / "skills_custom",
                home / ".agents" / "skills",
            ),
            quarantine_root=tools_root / "skills_quarantine",
        )

    def repo_skill_roots(self, repo: Path) -> tuple[Path, ...]:
        return (*self.skill_roots, Path(repo) / ".agents" / "skills")

    def directory_skeleton(self) -> tuple[Path, ...]:
        return (
            self.tools_root / "base",
            self.tools_root / "hooks",
            self.tools_root / "skills",
            self.tools_root / "skills_approved",
            self.tools_root / "skills_custom",
            self.tools_root / "skills_quarantine",
            self.tools_root / "templates",
            self.tools_root / "token_tools",
            self.runtime_root,
            self.workspace_manifest_root,
            self.workspace_mirror_root,
        )


def _interpolate(value: str, context: dict[str, str]) -> str:
    return string.Template(value).safe_substitute(context)


def _resolve_config_path(explicit_path: str | os.PathLike | None) -> Path:
    if explicit_path:
        return Path(explicit_path).expanduser()
    env_path = os.environ.get(ENV_CONFIG_PATH)
    if env_path:
        return Path(env_path).expanduser()
    return default_config_path()


def load_config(explicit_path: str | os.PathLike | None = None) -> Config:
    """Resolve a Config, applying the documented precedence order."""
    path = _resolve_config_path(explicit_path)
    base = Config.defaults()
    if not path.is_file():
        return base
    data = yaml.safe_load(path.read_text()) or {}
    return _apply_overrides(base, data, source_path=path)


def _apply_overrides(base: Config, data: dict, source_path: Path) -> Config:
    context = {"HOME": str(Path.home()), "USER": os.environ.get("USER", os.environ.get("USERNAME", ""))}

    def resolved(key: str, fallback: Path) -> Path:
        raw = data.get(key)
        if raw is None:
            value = fallback
        else:
            value = Path(_interpolate(str(raw), context)).expanduser()
        context[key] = str(value)
        return value

    user_root = resolved("user_root", base.user_root)
    account_home = resolved("account_home", base.account_home)
    tools_root = resolved("tools_root", base.tools_root)
    runtime_root = resolved("runtime_root", base.runtime_root)
    workspace_manifest_root = resolved("workspace_manifest_root", base.workspace_manifest_root)
    workspace_mirror_root = resolved("workspace_mirror_root", base.workspace_mirror_root)
    quarantine_root = resolved("quarantine_root", tools_root / "skills_quarantine")

    raw_skill_roots = data.get("skill_roots")
    if raw_skill_roots:
        skill_roots = tuple(Path(_interpolate(str(p), context)).expanduser() for p in raw_skill_roots)
    else:
        skill_roots = (
            tools_root / "skills",
            tools_root / "skills_approved",
            tools_root / "skills_custom",
            account_home / ".agents" / "skills",
        )

    engines = dict(base.engines)
    engines.update(data.get("engines", {}) or {})

    return Config(
        user_root=user_root,
        account_home=account_home,
        tools_root=tools_root,
        runtime_root=runtime_root,
        workspace_manifest_root=workspace_manifest_root,
        workspace_mirror_root=workspace_mirror_root,
        skill_roots=skill_roots,
        quarantine_root=quarantine_root,
        stale_retention_days=int(data.get("stale_retention_days", base.stale_retention_days)),
        default_access_mode=str(data.get("default_access_mode", base.default_access_mode)),
        engines=engines,
        source_path=source_path,
    )


def write_config(config: Config, path: Path) -> None:
    payload = {
        "user_root": str(config.user_root),
        "account_home": str(config.account_home),
        "tools_root": str(config.tools_root),
        "runtime_root": str(config.runtime_root),
        "workspace_manifest_root": str(config.workspace_manifest_root),
        "workspace_mirror_root": str(config.workspace_mirror_root),
        "skill_roots": [str(p) for p in config.skill_roots],
        "quarantine_root": str(config.quarantine_root),
        "stale_retention_days": config.stale_retention_days,
        "default_access_mode": config.default_access_mode,
        "engines": config.engines,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False))


# --- pure filesystem/repo helpers -------------------------------------------------


def timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def hostname_short() -> str:
    return socket.gethostname().split(".")[0]


def abs_path(path: str | os.PathLike) -> Path:
    return Path(path).expanduser().resolve()


def sanitize_name(name: str) -> str:
    return _NAME_INVALID_RE.sub("_", name)


def is_git_repo(path: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def repo_root(path: str | os.PathLike | None = None) -> Path:
    start = (Path(path) if path is not None else Path.cwd()).expanduser().resolve()
    try:
        result = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        result = None
    if result is not None and result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip()).resolve()
    return start


def looks_like_project_dir(path: Path) -> bool:
    return any((path / marker).is_file() for marker in _PROJECT_MARKERS)


def repo_name(repo: Path | None = None) -> str:
    return repo_root(repo).name


def repo_slug(repo: Path | None = None) -> str:
    root = repo_root(repo)
    base = _SLUG_INVALID_RE.sub("_", root.name.lower())
    digest = hashlib.sha1(str(root).encode("utf-8")).hexdigest()[:10]
    return f"{base}__{digest}"


def is_home_like_dir(path: Path, config: Config) -> bool:
    """True if `path` is, or is nested inside, a directory the tool treats as
    "home" (user_root/account_home/$HOME). Unlike the bash predecessor's
    exact-match-only check, this also refuses a *subdirectory* of home and any
    symlink that resolves into it, since those are just as unsafe to bulk-scan.
    """
    resolved = Path(path).expanduser().resolve()
    candidates = {config.user_root.resolve(), config.account_home.resolve(), Path.home().resolve()}
    for candidate in candidates:
        if resolved == candidate or candidate in resolved.parents:
            return True
    return False


def backup_path(path: Path) -> Path | None:
    if not path.exists() and not path.is_symlink():
        return None
    backup = path.with_name(f"{path.name}.bak.{timestamp()}")
    if path.is_dir() and not path.is_symlink():
        shutil.copytree(path, backup, symlinks=True)
    else:
        shutil.copy2(path, backup, follow_symlinks=False)
    return backup


__all__ = [
    "Config",
    "DEFAULT_ACCESS_MODE",
    "DEFAULT_STALE_RETENTION_DAYS",
    "ENV_CONFIG_PATH",
    "abs_path",
    "backup_path",
    "default_config_path",
    "hostname_short",
    "is_git_repo",
    "is_home_like_dir",
    "load_config",
    "looks_like_project_dir",
    "repo_name",
    "repo_root",
    "repo_slug",
    "sanitize_name",
    "timestamp",
    "write_config",
    "xdg_config_home",
]
