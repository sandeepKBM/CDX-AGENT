"""Hook script installation and hooks.json generation for both engines.

Fixes the confirmed **A6**/**D3** gap in the bash predecessor:
`write_repo_hooks_json`/`write_runtime_hooks_json` only wired 3 of the 4
available hook scripts into the generated hooks.json (dropping
`token_risk_warn.py`), even though all 4 scripts were symlinked into the
hooks directory and the tool's own `hooks.example.json` template correctly
wires all 4. Here, `build_hooks_payload` is the single source of truth for
the wired script set, used for both the repo and runtime install paths and
for both the codex and claude engines, so this class of drift can't recur.

Note: this hook layer is defense-in-depth only, not the primary safety
boundary -- it's simple regex matching over command text (see
`pre_tool_use_policy.py`) and can be bypassed by a sufficiently different
phrasing of a destructive command. The real boundary is the launch sandbox
mode (see `runtime.py`/`launch.py`); prefer `--safe` over `--full` for any
repo where destructive-command risk actually matters.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .config import Config, backup_path, load_config, repo_root

HOOK_SCRIPTS: tuple[str, ...] = (
    "pre_tool_use_policy.py",
    "post_tool_use_review.py",
    "stop_summary.py",
    "token_risk_warn.py",
)

Engine = Literal["codex", "claude"]


def build_hooks_payload(hooks_dir: Path) -> dict:
    """Wires all 4 available scripts (fix A6/D3) -- previously only 3 were
    referenced in the generated JSON despite all 4 being installed."""
    return {
        "PreToolUse": [
            {
                "matcher": "Bash|Edit|Write|apply_patch",
                "hooks": [
                    {
                        "type": "command",
                        "command": f'/usr/bin/python3 "{hooks_dir / "pre_tool_use_policy.py"}"',
                        "timeout": 30,
                        "statusMessage": "Checking command or edit safety",
                    },
                    {
                        "type": "command",
                        "command": f'/usr/bin/python3 "{hooks_dir / "token_risk_warn.py"}"',
                        "timeout": 30,
                        "statusMessage": "Checking token-risk command patterns",
                    },
                ],
            }
        ],
        "PostToolUse": [
            {
                "matcher": "Bash",
                "hooks": [
                    {
                        "type": "command",
                        "command": f'/usr/bin/python3 "{hooks_dir / "post_tool_use_review.py"}"',
                        "timeout": 30,
                        "statusMessage": "Reviewing Bash side effects",
                    }
                ],
            }
        ],
        "Stop": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": f'/usr/bin/python3 "{hooks_dir / "stop_summary.py"}"',
                        "timeout": 30,
                        "statusMessage": "Summarizing session changes",
                    }
                ]
            }
        ],
    }


def referenced_scripts(payload: dict) -> set[str]:
    """Which script filenames a generated hooks.json payload actually wires,
    used by the regression test that would have caught the A6 omission."""
    names: set[str] = set()
    for entries in payload.values():
        for entry in entries:
            for hook in entry.get("hooks", []):
                command = hook["command"]
                parts = command.split('"')
                if len(parts) >= 2:
                    names.add(Path(parts[1]).name)
    return names


@dataclass(frozen=True)
class HookInstallResult:
    hooks_dir: Path
    hooks_json_path: Path
    linked_scripts: tuple[Path, ...]


def install_hook_scripts(hooks_source_root: Path, dst_dir: Path) -> list[Path]:
    dst_dir.mkdir(parents=True, exist_ok=True)
    linked: list[Path] = []
    for script in HOOK_SCRIPTS:
        src = hooks_source_root / script
        target = dst_dir / script
        if not src.is_file():
            continue
        if target.exists() or target.is_symlink():
            backup_path(target)
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        target.symlink_to(src)
        linked.append(target)
    return linked


def write_hooks_json(hooks_dir: Path, hooks_json_path: Path) -> None:
    if hooks_json_path.exists():
        backup_path(hooks_json_path)
    hooks_json_path.parent.mkdir(parents=True, exist_ok=True)
    payload = build_hooks_payload(hooks_dir)
    hooks_json_path.write_text(json.dumps(payload, indent=2) + "\n")


def hooks_locations_for_repo(repo: Path, engine: Engine) -> tuple[Path, Path]:
    engine_dir = ".codex" if engine == "codex" else ".claude"
    base = repo / engine_dir
    return base / "hooks", base / "hooks.json"


def hooks_locations_for_runtime(runtime_dir: Path) -> tuple[Path, Path]:
    # Runtime dirs are already engine-isolated (see runtime.py's A1 fix), so
    # both engines use the same relative layout inside their own runtime dir.
    return runtime_dir / "hooks", runtime_dir / "hooks.json"


def install_hooks_for_repo(config: Config, repo: Path, engine: Engine = "codex") -> HookInstallResult:
    hooks_dir, hooks_json_path = hooks_locations_for_repo(repo, engine)
    linked = install_hook_scripts(config.tools_root / "hooks", hooks_dir)
    write_hooks_json(hooks_dir, hooks_json_path)
    return HookInstallResult(hooks_dir=hooks_dir, hooks_json_path=hooks_json_path, linked_scripts=tuple(linked))


def install_hooks_for_runtime(config: Config, runtime_dir: Path, engine: Engine = "codex") -> HookInstallResult:
    hooks_dir, hooks_json_path = hooks_locations_for_runtime(runtime_dir)
    linked = install_hook_scripts(config.tools_root / "hooks", hooks_dir)
    write_hooks_json(hooks_dir, hooks_json_path)
    return HookInstallResult(hooks_dir=hooks_dir, hooks_json_path=hooks_json_path, linked_scripts=tuple(linked))


# --- CLI commands --------------------------------------------------------------------


def command_install_hooks(args) -> int:
    cfg = load_config(getattr(args, "config", None))
    repo = repo_root(Path(args.repo))
    result = install_hooks_for_repo(cfg, repo, engine=args.engine)
    print(f"hooks_json={result.hooks_json_path}")
    print(f"scripts_linked={len(result.linked_scripts)}")
    return 0


__all__ = [
    "HOOK_SCRIPTS",
    "Engine",
    "HookInstallResult",
    "build_hooks_payload",
    "command_install_hooks",
    "hooks_locations_for_repo",
    "hooks_locations_for_runtime",
    "install_hook_scripts",
    "install_hooks_for_repo",
    "install_hooks_for_runtime",
    "referenced_scripts",
    "write_hooks_json",
]
