"""Per-repo runtime provisioning: isolated runtime directories, content-hash
config/auth sync, and stale-runtime reaping.

Fixes two confirmed safety bugs in the bash predecessor (`bin/cdx-agent`):

- **A1**: `--safe` and the default `--full` launch shared one runtime
  directory for the same repo (config.toml/auth.json/lock/skills), because
  bash's `current_codex_home()` had both branches call `runtime_codex_home`.
  Here `access_mode` and `engine` are folded into the runtime path itself, so
  every combination gets a physically separate directory.
- **A7**: `ensure_runtime_config`/`copy_auth_if_needed` only wrote a file if it
  was absent, so edits to the user's real `~/.codex/config.toml` or a fresh
  `codex login` never propagated into an already-provisioned runtime. Here a
  content hash is tracked per synced file and re-synced whenever the source
  changes, with conflict detection if the runtime copy was hand-edited.

Also implements **A2**: stale runtime directories (moved aside on session
cancellation, see `session.py`) used to retain copied credentials forever.
`reap_stale_runtimes` reports their age on every call and can remove them past
a configurable retention window.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .config import Config, hostname_short, load_config, repo_root, repo_slug

Engine = Literal["codex", "claude"]
AccessMode = Literal["full", "safe"]

DEFAULT_MODEL_LINE = 'model = "gpt-5.5"'
DEFAULT_REASONING_LINE = 'model_reasoning_effort = "xhigh"'
HASH_STATE_FILENAME = ".source_hashes.json"
STALE_SUFFIX_MARKER = ".stale."
_CREDENTIAL_FILENAMES = ("auth.json", "claude_credentials.json")


@dataclass(frozen=True)
class RuntimeContext:
    repo: Path
    engine: Engine
    access_mode: AccessMode
    runtime_dir: Path
    config_path: Path
    auth_path: Path
    lock_path: Path
    skills_dir: Path
    agents_path: Path


def runtime_home(config: Config, repo: Path, access_mode: AccessMode = "safe", engine: Engine = "codex") -> Path:
    """A1 fix: engine + access_mode are part of the path, so runtimes never collide."""
    host = hostname_short()
    slug = repo_slug(repo)
    return config.runtime_root / host / engine / access_mode / slug


def legacy_runtime_home(config: Config, repo: Path) -> Path:
    """The bash predecessor's pre-A1 shared path. Used only to detect and
    migrate old state, never as an active runtime location."""
    host = hostname_short()
    slug = repo_slug(repo)
    return config.runtime_root / host / slug


def runtime_context(config: Config, repo: Path, access_mode: AccessMode = "safe", engine: Engine = "codex") -> RuntimeContext:
    resolved_repo = repo_root(repo)
    runtime_dir = runtime_home(config, resolved_repo, access_mode, engine)
    is_codex = engine == "codex"
    # Codex reads $CODEX_HOME/skills because CODEX_HOME redirects its whole
    # config/skills/auth home to runtime_dir (see env_overrides in launch.py).
    # Claude Code has no equivalent single-env-var redirect; it only picks up
    # skills from `<added-dir>/.claude/skills/*/SKILL.md` when that directory
    # is passed via `--add-dir` (empirically verified) -- so the skills_dir
    # layout itself must differ per engine, not just the launch flags.
    skills_dir = runtime_dir / "skills" if is_codex else runtime_dir / ".claude" / "skills"
    return RuntimeContext(
        repo=resolved_repo,
        engine=engine,
        access_mode=access_mode,
        runtime_dir=runtime_dir,
        config_path=runtime_dir / ("config.toml" if is_codex else "claude_settings.json"),
        auth_path=runtime_dir / ("auth.json" if is_codex else "claude_credentials.json"),
        lock_path=runtime_dir / ".cdx-session.lock",
        skills_dir=skills_dir,
        agents_path=runtime_dir / ("AGENTS.md" if is_codex else "CLAUDE.md"),
    )


def migrate_legacy_runtime(config: Config, repo: Path, dry_run: bool = False) -> Path | None:
    """One-time migration for A1: copy the bash tool's shared runtime dir into
    the new `codex/full` slot, so a subsequent `safe` launch starts clean
    instead of silently inheriting full-access config/auth/skills state.
    Returns the new path if a migration happened (or would happen, dry-run),
    else None (nothing to migrate, or target already provisioned)."""
    legacy = legacy_runtime_home(config, repo)
    if not legacy.is_dir():
        return None
    target = runtime_home(config, repo, access_mode="full", engine="codex")
    if target.exists():
        return None
    if dry_run:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(legacy, target, symlinks=True)
    return target


# --- content-hash sync (A7) --------------------------------------------------------


@dataclass(frozen=True)
class SyncResult:
    key: str
    action: Literal["created", "updated", "unchanged", "conflict", "skipped"]
    detail: str = ""


def _file_hash(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_hash_state(runtime_dir: Path) -> dict:
    state_path = runtime_dir / HASH_STATE_FILENAME
    if not state_path.is_file():
        return {}
    try:
        return json.loads(state_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_hash_state(runtime_dir: Path, state: dict) -> None:
    state_path = runtime_dir / HASH_STATE_FILENAME
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True))


def _clear_hash_entry(runtime_dir: Path, key: str) -> None:
    state = _load_hash_state(runtime_dir)
    if key in state:
        del state[key]
        _save_hash_state(runtime_dir, state)


def _sync_bytes(runtime_dir: Path, key: str, content: bytes, dest: Path, dry_run: bool) -> SyncResult:
    state = _load_hash_state(runtime_dir)
    entry = state.get(key, {})
    source_hash = hashlib.sha256(content).hexdigest()
    dest_hash = _file_hash(dest)
    last_source_hash = entry.get("source_hash")
    last_dest_hash = entry.get("dest_hash")

    if dest_hash is not None and last_dest_hash is not None and dest_hash != last_dest_hash:
        if source_hash != last_source_hash:
            return SyncResult(
                key,
                "conflict",
                "runtime copy was hand-edited and the source also changed; run resync() to force",
            )
        return SyncResult(key, "unchanged", "runtime copy hand-edited, source unchanged; leaving as-is")

    if dest_hash is not None and source_hash == last_source_hash:
        return SyncResult(key, "unchanged")

    action = "updated" if dest_hash is not None else "created"
    if dry_run:
        return SyncResult(key, action, "dry-run")

    runtime_dir.mkdir(parents=True, exist_ok=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)
    dest.chmod(0o600)
    state[key] = {
        "source_hash": source_hash,
        "dest_hash": _file_hash(dest),
        "synced_at": time.time(),
    }
    _save_hash_state(runtime_dir, state)
    return SyncResult(key, action)


def sync_runtime_file(runtime_dir: Path, key: str, source: Path, dest: Path, dry_run: bool = False) -> SyncResult:
    if not source.is_file():
        return SyncResult(key, "skipped", "source missing")
    return _sync_bytes(runtime_dir, key, source.read_bytes(), dest, dry_run)


def sync_rendered_text(runtime_dir: Path, key: str, content: str, dest: Path, dry_run: bool = False) -> SyncResult:
    return _sync_bytes(runtime_dir, key, content.encode("utf-8"), dest, dry_run)


def _extract_toml_key(path: Path, key: str) -> str | None:
    """Return a canonical `key = <value>` line for a TOP-LEVEL key in the
    given TOML file, or None. Parses with tomllib so spacing variants
    (`model="x"`, `model  =  "x"`) work and keys inside tables
    (`[profiles.x] model = ...`) are correctly NOT promoted to top level --
    the previous line-prefix match failed both ways. Falls back to the old
    prefix scan only if the file isn't valid TOML."""
    if not path.is_file():
        return None
    try:
        data = tomllib.loads(path.read_text())
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError):
        prefix = f"{key} = "
        for line in path.read_text(errors="ignore").splitlines():
            if line.startswith(prefix):
                return line
        return None
    if key not in data:
        return None
    value = data[key]
    if isinstance(value, str):
        return f'{key} = "{value}"'
    if isinstance(value, bool):
        return f"{key} = {str(value).lower()}"
    if isinstance(value, (int, float)):
        return f"{key} = {value}"
    return None  # tables/arrays aren't representable as a single config line


def render_codex_config(config: Config, repo: Path) -> str:
    account_config = config.account_home / ".codex" / "config.toml"
    model_line = _extract_toml_key(account_config, "model") or DEFAULT_MODEL_LINE
    reasoning_line = _extract_toml_key(account_config, "model_reasoning_effort") or DEFAULT_REASONING_LINE
    resolved_repo = repo_root(repo)
    return (
        f"{model_line}\n"
        f"{reasoning_line}\n"
        "\n"
        f'[projects."{config.user_root}"]\n'
        'trust_level = "trusted"\n'
        "\n"
        f'[projects."{resolved_repo}"]\n'
        'trust_level = "trusted"\n'
    )


def sync_runtime_config(config: Config, rctx: RuntimeContext, dry_run: bool = False) -> SyncResult:
    if rctx.engine == "codex":
        content = render_codex_config(config, rctx.repo)
        return sync_rendered_text(rctx.runtime_dir, "config", content, rctx.config_path, dry_run=dry_run)
    source = config.account_home / ".claude" / "settings.json"
    return sync_runtime_file(rctx.runtime_dir, "config", source, rctx.config_path, dry_run=dry_run)


def sync_runtime_auth(config: Config, rctx: RuntimeContext, dry_run: bool = False) -> SyncResult:
    if rctx.engine == "claude":
        # Claude Code manages its own auth (ANTHROPIC_API_KEY / OAuth /
        # keychain via ~/.claude) -- unlike Codex, it does not read
        # credentials from an arbitrary CODEX_HOME-style copied file, so
        # there's nothing for this to sync. Skipping also avoids needlessly
        # placing credential-shaped material in a directory that gets broad
        # tool-read access via --add-dir.
        return SyncResult("auth", "skipped", "claude manages its own auth; nothing to sync")
    source = config.account_home / ".codex" / "auth.json"
    return sync_runtime_file(rctx.runtime_dir, "auth", source, rctx.auth_path, dry_run=dry_run)


def resync(config: Config, rctx: RuntimeContext) -> list[SyncResult]:
    """Explicit force-resync escape hatch (`cdx-agent sync-runtime --force`):
    re-pull from source now regardless of conflict detection, e.g. right after
    a `codex login`/Claude re-auth refreshed the source credentials."""
    _clear_hash_entry(rctx.runtime_dir, "config")
    _clear_hash_entry(rctx.runtime_dir, "auth")
    return [sync_runtime_config(config, rctx), sync_runtime_auth(config, rctx)]


def provision_runtime(
    config: Config,
    repo: Path,
    access_mode: AccessMode = "safe",
    engine: Engine = "codex",
    dry_run: bool = False,
) -> RuntimeContext:
    """High-level entry point: resolve the (isolated, per A1) runtime dir for
    this repo/access_mode/engine and content-hash-sync (per A7) its config and
    auth files. Does not acquire the session lock or link skills — see
    `session.py` and `skills.py`."""
    rctx = runtime_context(config, repo, access_mode=access_mode, engine=engine)
    if not dry_run:
        rctx.runtime_dir.mkdir(parents=True, exist_ok=True)
        _remove_legacy_claude_skills_dir(config, rctx)
    sync_runtime_config(config, rctx, dry_run=dry_run)
    sync_runtime_auth(config, rctx, dry_run=dry_run)
    return rctx


def _remove_legacy_claude_skills_dir(config: Config, rctx: RuntimeContext) -> None:
    """Claude runtimes originally linked skills into a top-level `skills/`
    (the codex layout) before the engine split moved them to
    `.claude/skills/` -- the only place Claude Code actually discovers them
    via `--add-dir`. The orphaned top-level dir never expires on its own and
    keeps feeding stale symlinks into the added directory, so prune it here.
    Contents are only symlinks (skills are linked, never copied), and removal
    is containment-checked to the runtime root."""
    if rctx.engine != "claude":
        return
    legacy = rctx.runtime_dir / "skills"
    if legacy == rctx.skills_dir or not legacy.is_dir() or legacy.is_symlink():
        return
    _safe_remove_runtime_tree(config, legacy)


# --- stale runtime reaping (A2) -----------------------------------------------------


@dataclass(frozen=True)
class ReapReport:
    path: Path
    age_days: float
    action: Literal["reaped", "pending", "kept"]


def find_stale_runtimes(config: Config) -> list[Path]:
    if not config.runtime_root.is_dir():
        return []
    return sorted(
        p
        for p in config.runtime_root.rglob(f"*{STALE_SUFFIX_MARKER}*")
        if p.is_dir() and STALE_SUFFIX_MARKER in p.name
    )


def _harden_credential_permissions(path: Path) -> None:
    for name in _CREDENTIAL_FILENAMES:
        candidate = path / name
        if candidate.is_file():
            candidate.chmod(0o600)


def _safe_remove_runtime_tree(config: Config, path: Path) -> None:
    resolved = path.resolve()
    root = config.runtime_root.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"Refusing to delete outside runtime root: {resolved}")
    shutil.rmtree(resolved)


def reap_stale_runtimes(config: Config, max_age_days: int | None = None, dry_run: bool = True) -> list[ReapReport]:
    """Report every stale runtime dir's age on every call (nothing disappears
    silently), and remove (contained to `config.runtime_root`, same guard
    style as `workspace_mirror.cdx_safe_remove_tree`) any older than the
    retention threshold when `dry_run=False`."""
    threshold = config.stale_retention_days if max_age_days is None else max_age_days
    now = time.time()
    reports = []
    for path in find_stale_runtimes(config):
        age_days = (now - path.stat().st_mtime) / 86400
        if age_days < threshold:
            if not dry_run:
                # Hardening chmods credential files, so it only belongs in
                # --apply mode -- the report-only default must not mutate
                # anything (it's documented as read-only).
                _harden_credential_permissions(path)
            reports.append(ReapReport(path, age_days, "kept"))
            continue
        if dry_run:
            reports.append(ReapReport(path, age_days, "pending"))
            continue
        _safe_remove_runtime_tree(config, path)
        reports.append(ReapReport(path, age_days, "reaped"))
    return reports


# --- CLI commands --------------------------------------------------------------------


def command_reap_stale_runtimes(args) -> int:
    cfg = load_config(getattr(args, "config", None))
    reports = reap_stale_runtimes(cfg, max_age_days=args.max_age_days, dry_run=not args.apply)
    if not reports:
        print("No stale runtime directories found.")
        return 0
    for report in reports:
        print(f"{report.action}\t{report.age_days:.1f}d\t{report.path}")
    return 0


def command_resync(args) -> int:
    cfg = load_config(getattr(args, "config", None))
    repo = repo_root(Path(args.repo))
    access_mode = "full" if args.full else "safe"
    rctx = runtime_context(cfg, repo, access_mode=access_mode, engine=args.engine)
    results = resync(cfg, rctx)
    for result in results:
        detail = f" ({result.detail})" if result.detail else ""
        print(f"{result.key}\t{result.action}{detail}")
    return 0


__all__ = [
    "AccessMode",
    "Engine",
    "ReapReport",
    "RuntimeContext",
    "SyncResult",
    "command_reap_stale_runtimes",
    "command_resync",
    "find_stale_runtimes",
    "legacy_runtime_home",
    "migrate_legacy_runtime",
    "provision_runtime",
    "reap_stale_runtimes",
    "render_codex_config",
    "resync",
    "runtime_context",
    "runtime_home",
    "sync_rendered_text",
    "sync_runtime_auth",
    "sync_runtime_config",
    "sync_runtime_file",
]
