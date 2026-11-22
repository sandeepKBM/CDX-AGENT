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
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .config import Config, backup_path, load_config, repo_root

HOOK_SCRIPTS: tuple[str, ...] = (
    "pre_tool_use_policy.py",
    "post_tool_use_review.py",
    "token_risk_warn.py",
    "subagent_claims.py",
    "agent_model_gate.py",
    "output_cap.py",
)
# Linked into runtimes (older settings files still name them) but no longer wired: token_risk_warn is a no-op (F15).
LINKED_ONLY = frozenset({"token_risk_warn.py"})

Engine = Literal["codex", "claude"]


def build_hooks_payload(hooks_dir: Path) -> dict:
    """Wires all 4 available scripts (fix A6/D3) -- previously only 3 were
    referenced in the generated JSON despite all 4 being installed."""
    return {
        "PreToolUse": [
            {
                "matcher": "Bash|apply_patch",  # F15: the policy returns at once for Edit/Write, so do not start it there
                "hooks": [
                    {
                        "type": "command",
                        "command": f'/usr/bin/python3 "{hooks_dir / "pre_tool_use_policy.py"}"',
                        "timeout": 30,
                        "statusMessage": "Checking command or edit safety",
                    },
                ],  # token_risk_warn.py is a no-op now (F15): pre_tool_use_policy emits the token warnings
            },
            {   # every subagent names type / model / why; overrides of codex_tools/base/agent_policy.json ask the user
                "matcher": "Agent|Task",
                "hooks": [
                    {
                        "type": "command",
                        "command": f'/usr/bin/python3 "{hooks_dir / "agent_model_gate.py"}"',
                        "timeout": 30,
                        "statusMessage": "Checking agent model policy",
                    }
                ],
            },
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
            },
            {   # long Bash output: full text saved to a file, the context gets head + errors + tail (CDX_RAW=1 opts out)
                "matcher": "Bash",
                "hooks": [
                    {
                        "type": "command",
                        "command": f'/usr/bin/python3 "{hooks_dir / "output_cap.py"}"',
                        "timeout": 30,
                        "statusMessage": "Capping long output",
                    }
                ],
            },
        ],
        # stop_summary.py (disabled by the user 2026-08-13) is no longer wired; the file stays for old runtimes.
        "SubagentStop": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": f'/usr/bin/python3 "{hooks_dir / "subagent_claims.py"}"',
                        "timeout": 15,
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
    claude_settings_path: Path | None = None


def install_hook_scripts(hooks_source_root: Path, dst_dir: Path) -> list[Path]:
    dst_dir.mkdir(parents=True, exist_ok=True)
    linked: list[Path] = []
    for script in HOOK_SCRIPTS:
        src = hooks_source_root / script
        target = dst_dir / script
        if not src.is_file():
            continue
        if target.is_symlink() and target.readlink() == src:
            # Already linked to the right source: relinking (and the backup it
            # takes) would just accumulate a .bak per launch forever.
            linked.append(target)
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
    payload = build_hooks_payload(hooks_dir)
    rendered = json.dumps(payload, indent=2) + "\n"
    if hooks_json_path.exists():
        if hooks_json_path.read_text(errors="ignore") == rendered:
            return  # unchanged: rewriting would accumulate a .bak per launch
        backup_path(hooks_json_path)
    hooks_json_path.parent.mkdir(parents=True, exist_ok=True)
    hooks_json_path.write_text(rendered)


def build_claude_settings_payload(hooks_dir: Path, base_settings_path: Path | None = None, env_file: Path | None = None,
                                  sandbox_file: Path | None = None) -> dict:
    """Claude Code loads hooks from a settings file's top-level "hooks" key
    (confirmed empirically: `claude --settings <path>` with
    `{"hooks": {...}}` fires the configured hooks) -- unlike Codex, which
    reads a bare hooks.json directly. Starts from the user's own synced
    settings (theme/model/etc, if present) so `--settings` doesn't shadow
    preferences the user would otherwise get by default, then adds hooks."""
    payload: dict = {}
    if base_settings_path is not None and base_settings_path.is_file():
        try:
            payload = json.loads(base_settings_path.read_text())
        except (json.JSONDecodeError, OSError):
            payload = {}
    payload["hooks"] = build_hooks_payload(hooks_dir)
    env_defaults = load_env_defaults(env_file)
    if env_defaults:
        payload["env"] = {**env_defaults, **(payload.get("env") or {})}       # the user's own env wins
    sandbox = load_sandbox(sandbox_file)
    if sandbox:
        payload["sandbox"] = {**(payload.get("sandbox") or {}), **sandbox}    # ours wins: it is the --safe guard
    return payload


def load_sandbox(sandbox_file: Path | None) -> dict:
    """--safe sandbox block (user ruling Q4, 2026-11-20) from tools_root/base/claude_sandbox_safe.json.

    Keys starting with '_' are comments. The file's "enabled" decides; CDX_SAFE_SANDBOX=1/0 overrides it (for the live
    test). Returns {} when disabled or missing, so the settings carry no sandbox key at all.
    """
    if sandbox_file is None:
        return {}
    try:
        data = json.loads(Path(sandbox_file).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    data = {k: v for k, v in data.items() if not k.startswith("_")}
    force = os.environ.get("CDX_SAFE_SANDBOX")
    enabled = force == "1" if force in ("0", "1") else bool(data.get("enabled"))
    if not enabled:
        return {}
    data["enabled"] = True
    return data


def load_env_defaults(env_file: Path | None) -> dict:
    """Shared Claude env defaults (tools_root/base/claude_env.json; keys starting with '_' are comments). None = no defaults."""
    if env_file is None:
        return {}
    try:
        data = json.loads(Path(env_file).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return {k: str(v) for k, v in data.items() if not k.startswith("_")}


AGENT_MARKER = "managed-by: cdx-agent"


def install_agent_defs(src_dir: Path, dst_dir: Path) -> list[Path]:
    """Copy shared subagent definitions (tools_root/base/claude_agents/*.md) into ~/.claude/agents. A file there that lacks the
    managed-by marker is the user's own and is never overwritten."""
    out: list[Path] = []
    if not src_dir.is_dir():
        return out
    dst_dir.mkdir(parents=True, exist_ok=True)
    for src in sorted(src_dir.glob("*.md")):
        dst = dst_dir / src.name
        text = src.read_text()
        if dst.exists():
            cur = dst.read_text(errors="ignore")
            if AGENT_MARKER not in cur or cur == text:
                continue
        dst.write_text(text)
        out.append(dst)
    return out


def claude_settings_path_for_runtime(runtime_dir: Path) -> Path:
    # Deliberately not "claude_settings.json" -- that name is already used by
    # runtime.py's synced copy of the user's real ~/.claude/settings.json
    # (RuntimeContext.config_path). This is the merged file actually passed
    # to `--settings` (base settings + hooks).
    return runtime_dir / "claude_hooks_settings.json"


def write_claude_settings(hooks_dir: Path, base_settings_path: Path | None, output_path: Path, env_file: Path | None = None,
                          sandbox_file: Path | None = None) -> None:
    payload = build_claude_settings_payload(hooks_dir, base_settings_path, env_file, sandbox_file)
    rendered = json.dumps(payload, indent=2) + "\n"
    if output_path.exists():
        if output_path.read_text(errors="ignore") == rendered:
            return  # unchanged: rewriting would accumulate a .bak per launch
        backup_path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered)


def hooks_locations_for_repo(repo: Path, engine: Engine) -> tuple[Path, Path]:
    engine_dir = ".codex" if engine == "codex" else ".claude"
    base = repo / engine_dir
    return base / "hooks", base / "hooks.json"


def hooks_locations_for_runtime(runtime_dir: Path) -> tuple[Path, Path]:
    # Runtime dirs are already engine-isolated (see runtime.py's A1 fix), so
    # both engines use the same relative layout inside their own runtime dir.
    return runtime_dir / "hooks", runtime_dir / "hooks.json"


def _entry_uses_dir(entry: dict, hooks_dir: Path) -> bool:
    prefix = str(hooks_dir)
    return any(prefix in str(h.get("command", "")) for h in entry.get("hooks", []) if isinstance(h, dict))


def merge_repo_claude_settings(hooks_dir: Path, settings_path: Path) -> bool:
    """Claude Code reads hooks from the "hooks" key of `<repo>/.claude/settings.json`, not from `.claude/hooks.json`.
    Merge ours into that key: other top-level keys and the user's own hook entries stay; entries that point into our
    hooks dir are replaced. The old file is backed up to `settings.json.bak.<date>` when it changes. Returns True if written."""
    existing: dict = {}
    if settings_path.is_file():
        try:
            loaded = json.loads(settings_path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError(f"{settings_path} is not valid JSON ({exc}); fix or move it, nothing was written") from exc
        if not isinstance(loaded, dict):
            raise ValueError(f"{settings_path} is not a JSON object; nothing was written")
        existing = loaded
    merged_hooks: dict = {}
    current = existing.get("hooks")
    if isinstance(current, dict):
        for event, entries in current.items():
            if isinstance(entries, list):
                merged_hooks[event] = [e for e in entries if not (isinstance(e, dict) and _entry_uses_dir(e, hooks_dir))]
    for event, entries in build_hooks_payload(hooks_dir).items():
        merged_hooks[event] = merged_hooks.get(event, []) + entries
    payload = {**existing, "hooks": merged_hooks}
    rendered = json.dumps(payload, indent=2) + "\n"
    if settings_path.exists():
        if settings_path.read_text(errors="ignore") == rendered:
            return False
        backup_path(settings_path)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(rendered)
    return True


def install_hooks_for_repo(config: Config, repo: Path, engine: Engine = "codex") -> HookInstallResult:
    hooks_dir, hooks_json_path = hooks_locations_for_repo(repo, engine)
    linked = install_hook_scripts(config.tools_root / "hooks", hooks_dir)
    if engine == "claude":
        settings_path = repo / ".claude" / "settings.json"
        merge_repo_claude_settings(hooks_dir, settings_path)
        return HookInstallResult(hooks_dir=hooks_dir, hooks_json_path=settings_path, linked_scripts=tuple(linked),
                                 claude_settings_path=settings_path)
    write_hooks_json(hooks_dir, hooks_json_path)
    return HookInstallResult(hooks_dir=hooks_dir, hooks_json_path=hooks_json_path, linked_scripts=tuple(linked))


def install_hooks_for_runtime(config: Config, runtime_dir: Path, engine: Engine = "codex",
                              access_mode: str = "full") -> HookInstallResult:
    hooks_dir, hooks_json_path = hooks_locations_for_runtime(runtime_dir)
    linked = install_hook_scripts(config.tools_root / "hooks", hooks_dir)
    write_hooks_json(hooks_dir, hooks_json_path)
    claude_settings_path = None
    if engine == "claude":
        claude_settings_path = claude_settings_path_for_runtime(runtime_dir)
        base_settings_path = runtime_dir / "claude_settings.json"
        sandbox_file = config.tools_root / "base" / "claude_sandbox_safe.json" if access_mode == "safe" else None
        write_claude_settings(hooks_dir, base_settings_path, claude_settings_path, config.tools_root / "base" / "claude_env.json",
                              sandbox_file)
        install_agent_defs(config.tools_root / "base" / "claude_agents", Path.home() / ".claude" / "agents")
    return HookInstallResult(
        hooks_dir=hooks_dir,
        hooks_json_path=hooks_json_path,
        linked_scripts=tuple(linked),
        claude_settings_path=claude_settings_path,
    )


# --- CLI commands --------------------------------------------------------------------


def command_install_hooks(args) -> int:
    cfg = load_config(getattr(args, "config", None))
    repo = repo_root(Path(args.repo))
    result = install_hooks_for_repo(cfg, repo, engine=args.engine)
    if result.claude_settings_path is not None:
        print(f"settings_json={result.claude_settings_path}")
    else:
        print(f"hooks_json={result.hooks_json_path}")
    print(f"scripts_linked={len(result.linked_scripts)}")
    return 0


__all__ = [
    "HOOK_SCRIPTS",
    "Engine",
    "HookInstallResult",
    "build_claude_settings_payload",
    "build_hooks_payload",
    "claude_settings_path_for_runtime",
    "command_install_hooks",
    "hooks_locations_for_repo",
    "hooks_locations_for_runtime",
    "install_hook_scripts",
    "install_hooks_for_repo",
    "install_hooks_for_runtime",
    "merge_repo_claude_settings",
    "referenced_scripts",
    "write_claude_settings",
    "write_hooks_json",
]
