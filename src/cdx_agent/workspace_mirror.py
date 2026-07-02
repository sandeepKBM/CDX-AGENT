"""Multi-repo workspace mirroring for GrapeRoot/Dual-Graph (`dg`).

Ports the bash predecessor's `cdx_run_dg_workspace`/`cdx_init_dg_workspace`/
`cdx_list_dg_workspaces`/`cdx_show_dg_workspace`/`cdx_clean_dg_workspace`/
`cdx_safe_remove_tree` family. Lowest safety-priority module in the
migration (it already had a real containment guard in bash), ported last
per the plan.

A workspace manifest (`<name>.paths`, plain text, one path per line, `#`
comments allowed) lists directories to mirror. `build_mirror` symlinks each
into `config.workspace_mirror_root/<name>/`, refusing (unless
`force_home_scan=True`) to mirror anything under a home-like directory, and
writes a `WORKSPACE_INDEX.md` warning that editing through the mirror edits
the original source paths.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import Config, abs_path, backup_path, is_home_like_dir, load_config, sanitize_name

DG_INSTALL_INSTRUCTIONS = (
    "dg not found on PATH.\n\n"
    "To inspect/install GrapeRoot/Dual-Graph safely:\n"
    "  git clone https://github.com/kunal12203/Codex-CLI-Compact.git\n"
    "  cd Codex-CLI-Compact\n"
    "  less README.md\n"
    "  less install.sh\n"
    "  find . -maxdepth 3 -type f | sort\n\n"
    "Do not run curl-pipe-bash. Install only after inspection."
)


def validate_workspace_name(name: str) -> bool:
    if not name or name in {".", ".."} or "/" in name:
        return False
    return all(c.isalnum() or c in "_.-" for c in name)


def workspace_name_from_spec(spec: str) -> str:
    spec_path = Path(spec)
    if spec_path.is_file():
        base = spec_path.name
        if base.endswith(".paths"):
            base = base[: -len(".paths")]
        return sanitize_name(base)
    if not validate_workspace_name(spec):
        raise ValueError(f"Invalid workspace name: {spec}")
    return spec


def workspace_manifest_path(config: Config, spec: str) -> Path:
    spec_path = Path(spec)
    if spec_path.is_file():
        return abs_path(spec_path)
    if not validate_workspace_name(spec):
        raise ValueError(f"Invalid workspace name: {spec}")
    return config.workspace_manifest_root / f"{spec}.paths"


def workspace_mirror_path(config: Config, spec: str) -> Path:
    return config.workspace_mirror_root / workspace_name_from_spec(spec)


@dataclass(frozen=True)
class WorkspaceEntry:
    resolved: Path
    raw: str


def workspace_entries(manifest: Path) -> list[WorkspaceEntry]:
    if not manifest.is_file():
        return []
    manifest_dir = manifest.parent
    entries: list[WorkspaceEntry] = []
    for raw_line in manifest.read_text().splitlines():
        stripped = raw_line.rstrip("\r").strip()
        if not stripped or stripped.startswith("#"):
            continue
        candidate = Path(stripped)
        resolved = abs_path(candidate) if candidate.is_absolute() else abs_path(manifest_dir / candidate)
        entries.append(WorkspaceEntry(resolved=resolved, raw=stripped))
    return entries


def need_dg() -> str | None:
    """Returns the resolved `dg` binary path, or None if not found. Mirrors
    bash's explicit refusal to curl-pipe-bash install it -- callers should
    surface `DG_INSTALL_INSTRUCTIONS` on None, not auto-install."""
    return shutil.which("dg")


def safe_remove_tree(config: Config, target: Path) -> None:
    """Containment-checked delete: refuses to remove anything outside
    `config.workspace_mirror_root` -- unchanged from the bash predecessor's
    `cdx_safe_remove_tree`, one of the few real guards it already had."""
    resolved = abs_path(target)
    root = config.workspace_mirror_root.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"Refusing to delete outside {root}: {resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)


def resolve_dg_root(config: Config, root: Path, force_home_scan: bool = False) -> Path:
    resolved = abs_path(root)
    if not resolved.is_dir():
        raise ValueError(f"DG root is not a directory: {resolved}")
    if is_home_like_dir(resolved, config) and not force_home_scan:
        raise ValueError(
            f"Refusing to run dg against a home-like directory: {resolved} "
            "(pass force_home_scan=True only if you really intend that scan)"
        )
    return resolved


@dataclass(frozen=True)
class InitResult:
    manifest: Path
    entries: tuple[Path, ...]
    action: str  # "created" | "would_create"


def init_workspace(
    config: Config, name: str, paths: list[Path], force: bool = False, dry_run: bool = False
) -> InitResult:
    if not paths:
        raise ValueError("No directories provided for workspace init.")
    if not validate_workspace_name(name):
        raise ValueError(f"Invalid workspace name: {name}")
    manifest = config.workspace_manifest_root / f"{name}.paths"
    resolved_paths = []
    for path in paths:
        resolved = abs_path(path)
        if not resolved.is_dir():
            raise ValueError(f"Workspace path is not a directory: {path}")
        resolved_paths.append(resolved)
    if manifest.exists() and not force:
        raise ValueError(f"Workspace manifest already exists: {manifest} (pass force=True to overwrite)")
    if dry_run:
        return InitResult(manifest=manifest, entries=tuple(resolved_paths), action="would_create")
    config.workspace_manifest_root.mkdir(parents=True, exist_ok=True)
    if manifest.exists():
        backup_path(manifest)
    manifest.write_text("\n".join(str(p) for p in resolved_paths) + "\n")
    return InitResult(manifest=manifest, entries=tuple(resolved_paths), action="created")


def list_workspaces(config: Config) -> list[tuple[str, Path]]:
    if not config.workspace_manifest_root.is_dir():
        return []
    return [(manifest.stem, manifest) for manifest in sorted(config.workspace_manifest_root.glob("*.paths"))]


@dataclass(frozen=True)
class ShowEntry:
    present: bool
    resolved: Path
    raw: str


def show_workspace(config: Config, spec: str) -> list[ShowEntry]:
    manifest = workspace_manifest_path(config, spec)
    return [ShowEntry(present=e.resolved.is_dir(), resolved=e.resolved, raw=e.raw) for e in workspace_entries(manifest)]


def clean_workspace(config: Config, spec: str) -> Path | None:
    mirror_root = workspace_mirror_path(config, spec)
    if not mirror_root.is_dir():
        return None
    safe_remove_tree(config, mirror_root)
    return mirror_root


@dataclass(frozen=True)
class MirrorLink:
    link_name: str
    target: Path
    raw: str


@dataclass(frozen=True)
class MirrorResult:
    name: str
    manifest: Path
    mirror_root: Path
    links: tuple[MirrorLink, ...]
    index_path: Path


def _unique_link_name(base: str, seen: dict[str, int]) -> str:
    count = seen.get(base, 0)
    name = base if count == 0 else f"{base}-{count + 1}"
    seen[base] = count + 1
    return name


def build_mirror(
    config: Config, spec: str, force_home_scan: bool = False, dry_run: bool = False
) -> MirrorResult:
    """Symlink-mirror every entry of a workspace manifest into
    `config.workspace_mirror_root/<name>/`, refusing home-like entries unless
    `force_home_scan=True`. Ported from `cdx_run_dg_workspace`'s mirror-build
    section (invoking `dg` itself is a separate concern, see `run_dg_workspace`)."""
    manifest = workspace_manifest_path(config, spec)
    name = workspace_name_from_spec(spec)
    mirror_root = workspace_mirror_path(config, spec)
    entries = workspace_entries(manifest)
    if not entries:
        raise ValueError(f"Workspace manifest has no entries: {manifest}")

    for entry in entries:
        if not entry.resolved.is_dir():
            raise ValueError(f"Workspace path is missing or not a directory: {entry.resolved} (from {entry.raw})")
        if is_home_like_dir(entry.resolved, config) and not force_home_scan:
            raise ValueError(f"Refusing to scan a home-like workspace path: {entry.resolved} (from {entry.raw})")

    seen: dict[str, int] = {}
    links = [
        MirrorLink(link_name=_unique_link_name(sanitize_name(entry.resolved.name), seen), target=entry.resolved, raw=entry.raw)
        for entry in entries
    ]
    index_path = mirror_root / "WORKSPACE_INDEX.md"

    if dry_run:
        return MirrorResult(name=name, manifest=manifest, mirror_root=mirror_root, links=tuple(links), index_path=index_path)

    if mirror_root.exists():
        safe_remove_tree(config, mirror_root)
    mirror_root.mkdir(parents=True, exist_ok=True)

    index_lines = [
        "# Workspace Index",
        "",
        f"- Workspace: {name}",
        f"- Manifest: {manifest}",
        f"- Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        "",
        "Warning: editing files through this mirror edits the original source paths.",
        "Map every change back to the original source path before editing.",
        "",
        "## Entries",
    ]
    for link in links:
        (mirror_root / link.link_name).symlink_to(link.target)
        index_lines.append(f"- `{link.link_name}` -> `{link.target}` (from `{link.raw}`)")
    index_path.write_text("\n".join(index_lines) + "\n")
    return MirrorResult(name=name, manifest=manifest, mirror_root=mirror_root, links=tuple(links), index_path=index_path)


def build_dg_command(dg_bin: str, root: Path, passthrough: list[str] | None = None) -> list[str]:
    return [dg_bin, str(root), *(passthrough or [])]


def run_dg(
    config: Config, root: Path, passthrough: list[str] | None = None, force_home_scan: bool = False, dry_run: bool = False
) -> int | None:
    dg_bin = need_dg()
    if dg_bin is None:
        raise RuntimeError(DG_INSTALL_INSTRUCTIONS)
    resolved_root = resolve_dg_root(config, root, force_home_scan=force_home_scan)
    command = build_dg_command(dg_bin, resolved_root, passthrough)
    if dry_run:
        return None
    return subprocess.run(command).returncode


def run_dg_workspace(
    config: Config, spec: str, passthrough: list[str] | None = None, force_home_scan: bool = False, dry_run: bool = False
) -> int | None:
    dg_bin = need_dg()
    if dg_bin is None:
        raise RuntimeError(DG_INSTALL_INSTRUCTIONS)
    mirror = build_mirror(config, spec, force_home_scan=force_home_scan, dry_run=dry_run)
    if dry_run:
        return None
    command = build_dg_command(dg_bin, mirror.mirror_root, passthrough)
    return subprocess.run(command).returncode


# --- CLI commands --------------------------------------------------------------------


def command_dg(args) -> int:
    cfg = load_config(getattr(args, "config", None))
    code = run_dg(
        cfg, Path(args.root), passthrough=args.passthrough, force_home_scan=args.force_home_scan, dry_run=args.dry_run
    )
    return code if code is not None else 0


def command_dg_workspace_init(args) -> int:
    cfg = load_config(getattr(args, "config", None))
    result = init_workspace(cfg, args.name, [Path(p) for p in args.paths], force=args.force, dry_run=args.dry_run)
    print(f"{result.action}: {result.manifest}")
    return 0


def command_dg_workspace_list(args) -> int:
    cfg = load_config(getattr(args, "config", None))
    workspaces = list_workspaces(cfg)
    if not workspaces:
        print(f"No workspace manifests found in {cfg.workspace_manifest_root}")
        return 0
    for name, manifest in workspaces:
        print(f"{name} -> {manifest}")
    return 0


def command_dg_workspace_show(args) -> int:
    cfg = load_config(getattr(args, "config", None))
    for entry in show_workspace(cfg, args.name):
        print(f"{'present' if entry.present else 'missing'}\t{entry.resolved}\t{entry.raw}")
    return 0


def command_dg_workspace_clean(args) -> int:
    cfg = load_config(getattr(args, "config", None))
    removed = clean_workspace(cfg, args.name)
    if removed is None:
        print(f"No generated mirror to remove for: {args.name}")
    else:
        print(f"Removed generated mirror: {removed}")
    return 0


def command_dg_workspace_run(args) -> int:
    cfg = load_config(getattr(args, "config", None))
    code = run_dg_workspace(
        cfg, args.name, passthrough=args.passthrough, force_home_scan=args.force_home_scan, dry_run=args.dry_run
    )
    return code if code is not None else 0


__all__ = [
    "DG_INSTALL_INSTRUCTIONS",
    "command_dg",
    "command_dg_workspace_clean",
    "command_dg_workspace_init",
    "command_dg_workspace_list",
    "command_dg_workspace_run",
    "command_dg_workspace_show",
    "InitResult",
    "MirrorLink",
    "MirrorResult",
    "ShowEntry",
    "WorkspaceEntry",
    "build_dg_command",
    "build_mirror",
    "clean_workspace",
    "init_workspace",
    "list_workspaces",
    "need_dg",
    "resolve_dg_root",
    "run_dg",
    "run_dg_workspace",
    "safe_remove_tree",
    "show_workspace",
    "validate_workspace_name",
    "workspace_entries",
    "workspace_manifest_path",
    "workspace_mirror_path",
    "workspace_name_from_spec",
]
