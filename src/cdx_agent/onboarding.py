"""User onboarding: `cdx-agent init-user`.

Detects `$HOME` (never a hardcoded literal username), writes a user-level
config file, creates the directory skeleton (`Config.directory_skeleton()`),
and seeds default AGENTS.md/hook content so a fresh install doesn't depend on
any specific existing user's `codex_tools` tree -- the concrete mechanism for
"installable by others" from Workstream C.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, replace
from pathlib import Path

from . import context_docs
from .config import Config, default_config_path, write_config

_ADOPTABLE_TOOLS_SUBDIRS = ("skills", "skills_approved", "skills_custom", "base", "templates", "hooks", "token_tools")

_DEFAULT_HOOK_SCRIPTS: dict[str, str] = {
    "pre_tool_use_policy.py": (
        "#!/usr/bin/env python3\n"
        "import json, re, sys\n"
        "BLOCK = (re.compile(r'rm\\s+-rf'), re.compile(r'git\\s+reset\\s+--hard'), re.compile(r'git\\s+clean\\s+-fdx'))\n"
        "def main():\n"
        "    payload = json.loads(sys.stdin.read() or '{}')\n"
        "    command = str((payload.get('tool_input') or {}).get('command', ''))\n"
        "    for pattern in BLOCK:\n"
        "        if pattern.search(command):\n"
        "            print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', "
        "'permissionDecision': 'deny', 'permissionDecisionReason': 'Blocked by cdx-agent hook.'}}))\n"
        "            return 0\n"
        "    return 0\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main())\n"
    ),
    "post_tool_use_review.py": "#!/usr/bin/env python3\nif __name__ == '__main__':\n    raise SystemExit(0)\n",
    "stop_summary.py": "#!/usr/bin/env python3\nif __name__ == '__main__':\n    raise SystemExit(0)\n",
    "token_risk_warn.py": "#!/usr/bin/env python3\nif __name__ == '__main__':\n    raise SystemExit(0)\n",
}


def _with_tools_root(config: Config, new_tools_root: Path) -> Config:
    return replace(
        config,
        tools_root=new_tools_root,
        skill_roots=(
            new_tools_root / "skills",
            new_tools_root / "skills_approved",
            new_tools_root / "skills_custom",
            config.account_home / ".agents" / "skills",
        ),
        quarantine_root=new_tools_root / "skills_quarantine",
    )


def _with_overrides(config: Config, user_root: Path | None, tools_root: Path | None) -> Config:
    if user_root is not None:
        # `Config.defaults()` derives runtime_root/workspace_manifest_root/
        # workspace_mirror_root from user_root at construction time; simply
        # replacing the `user_root` field alone (without recomputing these)
        # would leave them pointing at the OLD root -- a real bug this fixes.
        new_user_root = Path(user_root)
        config = replace(
            config,
            user_root=new_user_root,
            runtime_root=new_user_root / "codex_runtime",
            workspace_manifest_root=new_user_root / ".cdx" / "workspaces",
            workspace_mirror_root=new_user_root / ".cdx" / "dg_workspaces",
        )
        if tools_root is None:
            config = _with_tools_root(config, new_user_root / "codex_tools")
    if tools_root is not None:
        config = _with_tools_root(config, Path(tools_root))
    return config


def _adopt_from_existing(source_tools_root: Path, config: Config) -> list[Path]:
    seeded: list[Path] = []
    for name in _ADOPTABLE_TOOLS_SUBDIRS:
        src = source_tools_root / name
        dst = config.tools_root / name
        if not src.is_dir():
            continue
        # `directory_skeleton()` pre-creates every adoptable subdir as an
        # empty directory before adoption runs, so `dst.exists()` alone can't
        # distinguish "the skeleton just touched this" from "the user already
        # has real content here" -- check for actual content instead.
        if dst.is_dir() and any(dst.iterdir()):
            continue
        if dst.is_dir():
            dst.rmdir()
        shutil.copytree(src, dst, symlinks=True)
        seeded.append(dst)
    return seeded


def _seed_builtin_defaults(config: Config) -> list[Path]:
    seeded: list[Path] = []
    agents_path = config.tools_root / "base" / "AGENTS.md"
    if not agents_path.is_file():
        agents_path.parent.mkdir(parents=True, exist_ok=True)
        agents_path.write_text(context_docs.DEFAULT_WORKING_RULES_TEMPLATE)
        seeded.append(agents_path)

    hooks_dir = config.tools_root / "hooks"
    for script_name, content in _DEFAULT_HOOK_SCRIPTS.items():
        target = hooks_dir / script_name
        if target.is_file():
            continue
        hooks_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        target.chmod(0o755)
        seeded.append(target)
    return seeded


@dataclass(frozen=True)
class InitUserResult:
    config_path: Path
    config: Config
    created_dirs: tuple[Path, ...]
    seeded_files: tuple[Path, ...]


def init_user(
    account_home: Path | None = None,
    user_root: Path | None = None,
    tools_root: Path | None = None,
    from_existing_user_tools_root: Path | None = None,
    config_path: Path | None = None,
    dry_run: bool = False,
) -> InitUserResult:
    home = account_home or Path.home()
    config = _with_overrides(Config.defaults(home=home), user_root, tools_root)
    target_config_path = config_path or default_config_path()

    if dry_run:
        return InitUserResult(
            config_path=target_config_path,
            config=config,
            created_dirs=config.directory_skeleton(),
            seeded_files=(),
        )

    write_config(config, target_config_path)
    for directory in config.directory_skeleton():
        directory.mkdir(parents=True, exist_ok=True)

    if from_existing_user_tools_root is not None:
        seeded = _adopt_from_existing(from_existing_user_tools_root, config)
    else:
        seeded = _seed_builtin_defaults(config)

    return InitUserResult(
        config_path=target_config_path,
        config=config,
        created_dirs=config.directory_skeleton(),
        seeded_files=tuple(seeded),
    )


# --- CLI commands --------------------------------------------------------------------


def command_init_user(args) -> int:
    result = init_user(
        user_root=Path(args.user_root) if args.user_root else None,
        tools_root=Path(args.tools_root) if args.tools_root else None,
        from_existing_user_tools_root=Path(args.from_existing_user) if args.from_existing_user else None,
        dry_run=args.dry_run,
    )
    print(f"config_path={result.config_path}")
    print(f"user_root={result.config.user_root}")
    print(f"tools_root={result.config.tools_root}")
    print(f"created_dirs={len(result.created_dirs)}")
    print(f"seeded_files={len(result.seeded_files)}")
    for f in result.seeded_files:
        print(f"  seeded: {f}")
    return 0


__all__ = ["InitUserResult", "command_init_user", "init_user"]
