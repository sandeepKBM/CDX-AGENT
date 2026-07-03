from __future__ import annotations

import argparse
import sys
from typing import Callable

from . import graph
from . import hooks
from . import launch
from . import onboarding
from . import runtime
from . import session
from . import skills
from . import token_tools
from . import workspace_mirror


LEGACY_ALIASES: dict[str, str] = {
    "--graph": "graph",
    "--context": "context",
    "--relevant": "relevant",
    "--impact": "impact",
    "--detect-deps": "detect-deps",
    "--init-workspace": "init-workspace",
    "--workspace-graph": "workspace-graph",
    "--workspace-doctor": "workspace-doctor",
    "--context-budget": "context-budget",
    "--summarize-output": "summarize-output",
    "--summarize-log": "summarize-log",
    "--safe-find": "safe-find",
    "--safe-rg": "safe-rg",
    "--safe-git-diff": "safe-git-diff",
    "--session-doctor": "session-doctor",
    "--cancel-active": "cancel-active",
    "--install-hooks": "install-hooks",
    "--skills-list": "skills-list",
    "--skills-audit": "skills-audit",
    "--validate-skills": "validate-skills",
    "graph": "graph",
    "context": "context",
    "relevant": "relevant",
    "impact": "impact",
    "detect-deps": "detect-deps",
    "init-workspace": "init-workspace",
    "workspace-graph": "workspace-graph",
    "workspace-doctor": "workspace-doctor",
    "context-budget": "context-budget",
    "summarize-output": "summarize-output",
    "summarize-log": "summarize-log",
    "safe-find": "safe-find",
    "safe-rg": "safe-rg",
    "safe-git-diff": "safe-git-diff",
    "launch": "launch",
    "session-doctor": "session-doctor",
    "cancel-active": "cancel-active",
    "reap-stale-runtimes": "reap-stale-runtimes",
    "resync": "resync",
    "install-hooks": "install-hooks",
    "sync-docs": "sync-docs",
    "skills-list": "skills-list",
    "skills-audit": "skills-audit",
    "validate-skills": "validate-skills",
    "init-user": "init-user",
    "dg": "dg",
    "dg-workspace-init": "dg-workspace-init",
    "dg-workspace-list": "dg-workspace-list",
    "dg-workspace-show": "dg-workspace-show",
    "dg-workspace-clean": "dg-workspace-clean",
    "dg-workspace-run": "dg-workspace-run",
}


def _general_help() -> str:
    return (
        "CDX-AGENT\n"
        "A clean, cross-platform packaging of the repository graph, launch orchestration,\n"
        "and token-saving helpers -- for both the Codex CLI and Claude Code.\n\n"
        "Usage:\n"
        "  cdx-agent launch [--repo PATH] [--engine codex|claude] [--full|--safe] [--dry-run]\n"
        "                   [--cancel-active] [-- PASSTHROUGH...]\n"
        "  cdx-agent --claude ...                 (shorthand for launch --engine claude)\n"
        "  cdx-agent session-doctor [--repo PATH] [--engine codex|claude] [--full|--safe]\n"
        "  cdx-agent cancel-active [--repo PATH] [--engine codex|claude] [--full|--safe] [--dry-run]\n"
        "  cdx-agent reap-stale-runtimes [--max-age-days N] [--apply]\n"
        "  cdx-agent resync [--repo PATH] [--engine codex|claude] [--full|--safe]\n"
        "  cdx-agent install-hooks [--repo PATH] [--engine codex|claude]\n"
        "  cdx-agent sync-docs [--repo PATH] [--engine codex|claude] [--adopt|--force]\n"
        "  cdx-agent skills-list|skills-audit|validate-skills [--repo PATH]\n"
        "  cdx-agent init-user [--user-root PATH] [--tools-root PATH] [--from-existing-user PATH]\n"
        "  cdx-agent dg --root PATH\n"
        "  cdx-agent dg-workspace-init --name NAME PATH [PATH ...]\n"
        "  cdx-agent dg-workspace-list|dg-workspace-show --name NAME|dg-workspace-clean --name NAME\n"
        "  cdx-agent dg-workspace-run --name NAME\n"
        "  cdx-agent --graph [--repo PATH] [--task TEXT] [--max-files N]\n"
        "  cdx-agent --context --repo PATH --task TEXT\n"
        "  cdx-agent --relevant --repo PATH --task TEXT\n"
        "  cdx-agent --impact --repo PATH --files FILE [FILE ...]\n"
        "  cdx-agent --detect-deps [--repo PATH]\n"
        "  cdx-agent --init-workspace [--repo PATH] [--yes]\n"
        "  cdx-agent --workspace-graph [--repo PATH]\n"
        "  cdx-agent --workspace-doctor [--repo PATH]\n"
        "  cdx-agent --context-budget [--repo PATH] [--max-files N]\n"
        "  cdx-agent --summarize-output INPUT\n"
        "  cdx-agent --summarize-log INPUT\n"
        "  cdx-agent --safe-find [PATH ...]\n"
        "  cdx-agent --safe-rg PATTERN [PATH ...]\n"
        "  cdx-agent --safe-git-diff [--repo PATH] [--file FILE]\n\n"
        "Legacy compatibility:\n"
        "  The leading --graph/--workspace-graph/--context-budget style is accepted.\n"
        "  The canonical package entrypoint is still: cdx-agent\n"
    )


def _parser_base(prog: str, description: str) -> argparse.ArgumentParser:
    return argparse.ArgumentParser(prog=prog, description=description)


def _graph_parser(mode: str) -> argparse.ArgumentParser:
    parser = _parser_base(f"cdx-agent {mode}", "Build or inspect repository graph data.")
    parser.add_argument("--repo", default=".", help="Repository root or current directory.")
    parser.add_argument("--max-files", type=int, default=20000, help="Maximum candidate files to scan.")
    parser.add_argument("--force-home-scan", action="store_true", help="Allow scanning a home-like directory.")
    return parser


def _build_parser(mode: str) -> argparse.ArgumentParser:
    parser = _graph_parser(mode)
    parser.add_argument("--task", default="", help="Task text used to rank relevant files.")
    return parser


def _task_graph_parser(mode: str) -> argparse.ArgumentParser:
    parser = _graph_parser(mode)
    parser.add_argument("--task", required=True, help="Task text used to rank relevant files.")
    return parser


def _impact_parser(mode: str) -> argparse.ArgumentParser:
    parser = _graph_parser(mode)
    parser.add_argument("--files", nargs="+", required=True, help="Files to inspect for reverse impact.")
    parser.add_argument("--depth", type=int, default=3, help="Transitive reverse-import depth (default 3).")
    return parser


def _workspace_parser(mode: str) -> argparse.ArgumentParser:
    parser = _graph_parser(mode)
    parser.add_argument("--yes", action="store_true", help="Skip confirmation when creating workspace.yaml.")
    return parser


def _context_budget_parser(mode: str) -> argparse.ArgumentParser:
    parser = _parser_base(f"cdx-agent {mode}", "Report files and directories most likely to waste context.")
    parser.add_argument("--repo", default=".", help="Repository root or current directory.")
    parser.add_argument("--max-files", type=int, default=50000, help="Maximum files to scan.")
    parser.add_argument("--force-home-scan", action="store_true", help="Allow scanning a home-like directory.")
    return parser


def _summarize_output_parser(mode: str) -> argparse.ArgumentParser:
    parser = _parser_base(f"cdx-agent {mode}", "Summarize command output with tracebacks and path hints.")
    parser.add_argument("input", help="Path to a log or output file.")
    parser.add_argument("--command", default="", help="Original command, if known.")
    parser.add_argument("--exit-code", type=int, default=0, help="Command exit code.")
    parser.add_argument("--head-lines", type=int, default=20, help="Number of leading lines to show.")
    parser.add_argument("--tail-lines", type=int, default=40, help="Number of trailing lines to show.")
    return parser


def _summarize_log_parser(mode: str) -> argparse.ArgumentParser:
    parser = _parser_base(f"cdx-agent {mode}", "Summarize a log file with traceback and signal hints.")
    parser.add_argument("input", help="Path to a log file.")
    parser.add_argument("--head-lines", type=int, default=40, help="Number of leading lines to show.")
    parser.add_argument("--tail-lines", type=int, default=80, help="Number of trailing lines to show.")
    return parser


def _safe_find_parser(mode: str) -> argparse.ArgumentParser:
    parser = _parser_base(f"cdx-agent {mode}", "Discover files while skipping generated or noisy trees.")
    parser.add_argument("paths", nargs="*", default=["."], help="Paths to search.")
    parser.add_argument("--max-results", type=int, default=200, help="Maximum sample paths to print.")
    return parser


def _safe_rg_parser(mode: str) -> argparse.ArgumentParser:
    parser = _parser_base(f"cdx-agent {mode}", "Run ripgrep with safer defaults and capped output.")
    parser.add_argument("pattern", help="Search pattern.")
    parser.add_argument("paths", nargs="*", default=["."], help="Search roots.")
    parser.add_argument("--max-lines", type=int, default=200, help="Maximum output lines to print.")
    return parser


def _safe_git_diff_parser(mode: str) -> argparse.ArgumentParser:
    parser = _parser_base(f"cdx-agent {mode}", "Show git diff stats first, then optional hunks.")
    parser.add_argument("--repo", default=".", help="Git repository root.")
    parser.add_argument("--file", dest="files", action="append", default=[], help="Optional file to show hunks for.")
    parser.add_argument("--show-hunks", action="store_true", help="Print full hunks for requested files.")
    return parser


def _launch_parser(mode: str) -> argparse.ArgumentParser:
    parser = _parser_base(f"cdx-agent {mode}", "Launch an agent engine (codex or claude) for a repo.")
    parser.add_argument("--repo", default=".", help="Repository root or current directory.")
    parser.add_argument("--engine", choices=["codex", "claude"], default=launch.DEFAULT_ENGINE, help="Which agent engine to launch.")
    parser.add_argument("--config", default=None, help="Explicit path to a cdx-agent user config file.")
    access = parser.add_mutually_exclusive_group()
    access.add_argument("--full", action="store_true", help="Full-access sandbox/permissions.")
    access.add_argument("--safe", action="store_true", help="Restricted sandbox/permissions.")
    parser.add_argument("--dry-run", action="store_true", help="Print what would happen without launching.")
    conflict = parser.add_mutually_exclusive_group()
    conflict.add_argument(
        "--cancel-active",
        action="store_true",
        help="Diagnose and cancel a conflicting active/stale session if found (kills it -- use for a stuck session).",
    )
    conflict.add_argument(
        "--secondary",
        action="store_true",
        help="Join an already-running session's runtime for a second concurrent window "
        "(e.g. a second terminal) instead of fighting over the lock or killing it.",
    )
    token_saver = parser.add_mutually_exclusive_group()
    token_saver.add_argument(
        "--token-saver",
        action="store_true",
        help="Include the token-saving rules block in the session's working-rules doc (off by default).",
    )
    token_saver.add_argument(
        "--no-token-saver",
        dest="token_saver",
        action="store_false",
        help="Omit the token-saving rules block (default).",
    )
    parser.set_defaults(token_saver=False)
    parser.add_argument("passthrough", nargs=argparse.REMAINDER, help="Extra args forwarded to the engine binary.")
    return parser


def _session_target_parser(mode: str) -> argparse.ArgumentParser:
    parser = _parser_base(f"cdx-agent {mode}", "Target a repo's session/runtime for a given engine + access mode.")
    parser.add_argument("--repo", default=".", help="Repository root or current directory.")
    parser.add_argument(
        "--engine", choices=["codex", "claude"], default=launch.DEFAULT_ENGINE, help="Engine whose runtime to target."
    )
    parser.add_argument("--config", default=None, help="Path to a cdx-agent config file (overrides discovery).")
    access = parser.add_mutually_exclusive_group()
    access.add_argument("--full", action="store_true", help="Target the full-access runtime.")
    access.add_argument("--safe", action="store_true", help="Target the safe (workspace-write) runtime.")
    return parser


def _cancel_active_parser(mode: str) -> argparse.ArgumentParser:
    parser = _session_target_parser(mode)
    parser.add_argument("--dry-run", action="store_true", help="Diagnose only; never signal any process.")
    return parser


def _reap_stale_runtimes_parser(mode: str) -> argparse.ArgumentParser:
    parser = _parser_base(f"cdx-agent {mode}", "Report and optionally remove stale runtime directories.")
    parser.add_argument("--config", default=None)
    parser.add_argument("--max-age-days", type=int, default=None)
    parser.add_argument(
        "--apply", action="store_true", help="Actually remove stale runtimes past the retention window (default: report only)."
    )
    return parser


def _install_hooks_parser(mode: str) -> argparse.ArgumentParser:
    parser = _parser_base(f"cdx-agent {mode}", "Install hook scripts and hooks.json for a repo.")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--engine", choices=["codex", "claude"], default=launch.DEFAULT_ENGINE)
    parser.add_argument("--config", default=None)
    return parser


def _sync_docs_parser(mode: str) -> argparse.ArgumentParser:
    parser = _parser_base(f"cdx-agent {mode}", "Sync AGENTS.md/CLAUDE.md for a repo from the canonical template.")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--engine", choices=["codex", "claude"], default=launch.DEFAULT_ENGINE)
    parser.add_argument("--config", default=None)
    parser.add_argument("--adopt", action="store_true", help="Append canonical rules after existing hand-written content.")
    parser.add_argument("--force", action="store_true", help="Overwrite existing hand-written content (a backup is taken first).")
    return parser


def _skills_parser(mode: str) -> argparse.ArgumentParser:
    parser = _parser_base(f"cdx-agent {mode}", "Discover, list, or audit skills across all roots.")
    parser.add_argument("--repo", default=None, help="Include this repo's .agents/skills as an additional root.")
    parser.add_argument("--config", default=None)
    return parser


def _init_user_parser(mode: str) -> argparse.ArgumentParser:
    parser = _parser_base(f"cdx-agent {mode}", "Onboard this account: write config, seed directory skeleton and defaults.")
    parser.add_argument("--user-root", default=None)
    parser.add_argument("--tools-root", default=None)
    parser.add_argument(
        "--from-existing-user", default=None, help="Path to another user's codex_tools root to adopt shareable assets from."
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _dg_parser(mode: str) -> argparse.ArgumentParser:
    parser = _parser_base(f"cdx-agent {mode}", "Run GrapeRoot/Dual-Graph (dg) against a single directory.")
    parser.add_argument("--root", required=True, help="Directory to run dg against.")
    parser.add_argument("--config", default=None)
    parser.add_argument("--force-home-scan", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("passthrough", nargs=argparse.REMAINDER)
    return parser


def _dg_workspace_init_parser(mode: str) -> argparse.ArgumentParser:
    parser = _parser_base(f"cdx-agent {mode}", "Create or overwrite a multi-folder workspace manifest.")
    parser.add_argument("--name", required=True, help="Workspace name (manifest filename stem).")
    parser.add_argument("paths", nargs="+", help="Member directories to include in the workspace.")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing manifest of the same name.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be written without writing it.")
    parser.add_argument("--config", default=None, help="Path to a cdx-agent config file (overrides discovery).")
    return parser


def _dg_workspace_list_parser(mode: str) -> argparse.ArgumentParser:
    parser = _parser_base(f"cdx-agent {mode}", "List configured workspace manifests.")
    parser.add_argument("--config", default=None)
    return parser


def _dg_workspace_name_parser(mode: str) -> argparse.ArgumentParser:
    parser = _parser_base(f"cdx-agent {mode}", "Target a named (or manifest-path) workspace.")
    parser.add_argument("--name", required=True, help="Workspace name or path to its manifest file.")
    parser.add_argument("--config", default=None, help="Path to a cdx-agent config file (overrides discovery).")
    return parser


def _dg_workspace_run_parser(mode: str) -> argparse.ArgumentParser:
    parser = _dg_workspace_name_parser(mode)
    parser.add_argument("--force-home-scan", action="store_true", help="Allow scanning a home-like directory.")
    parser.add_argument("--dry-run", action="store_true", help="Show the dg invocation without running it.")
    parser.add_argument("passthrough", nargs=argparse.REMAINDER, help="Extra args forwarded to dg.")
    return parser


def _dispatch_table() -> dict[str, tuple[Callable[[str], argparse.ArgumentParser], Callable[[argparse.Namespace], int]]]:
    return {
        "graph": (_build_parser, graph.command_build),
        "context": (_task_graph_parser, graph.command_context),
        "relevant": (_task_graph_parser, graph.command_relevant),
        "impact": (_impact_parser, graph.command_impact),
        "detect-deps": (_graph_parser, graph.command_detect_deps),
        "init-workspace": (_workspace_parser, graph.command_init_workspace),
        "workspace-graph": (_graph_parser, graph.command_workspace_graph),
        "workspace-doctor": (_graph_parser, graph.command_workspace_doctor),
        "context-budget": (_context_budget_parser, token_tools.command_context_budget),
        "summarize-output": (_summarize_output_parser, token_tools.command_summarize_output),
        "summarize-log": (_summarize_log_parser, token_tools.command_summarize_log),
        "safe-find": (_safe_find_parser, token_tools.command_safe_find),
        "safe-rg": (_safe_rg_parser, token_tools.command_safe_rg),
        "safe-git-diff": (_safe_git_diff_parser, token_tools.command_safe_git_diff),
        "launch": (_launch_parser, launch.command_launch),
        "session-doctor": (_session_target_parser, session.command_session_doctor),
        "cancel-active": (_cancel_active_parser, session.command_cancel_active),
        "reap-stale-runtimes": (_reap_stale_runtimes_parser, runtime.command_reap_stale_runtimes),
        "resync": (_session_target_parser, runtime.command_resync),
        "install-hooks": (_install_hooks_parser, hooks.command_install_hooks),
        "sync-docs": (_sync_docs_parser, launch.command_sync_docs),
        "skills-list": (_skills_parser, skills.command_skills_list),
        "skills-audit": (_skills_parser, skills.command_skills_audit),
        "validate-skills": (_skills_parser, skills.command_validate_skills),
        "init-user": (_init_user_parser, onboarding.command_init_user),
        "dg": (_dg_parser, workspace_mirror.command_dg),
        "dg-workspace-init": (_dg_workspace_init_parser, workspace_mirror.command_dg_workspace_init),
        "dg-workspace-list": (_dg_workspace_list_parser, workspace_mirror.command_dg_workspace_list),
        "dg-workspace-show": (_dg_workspace_name_parser, workspace_mirror.command_dg_workspace_show),
        "dg-workspace-clean": (_dg_workspace_name_parser, workspace_mirror.command_dg_workspace_clean),
        "dg-workspace-run": (_dg_workspace_run_parser, workspace_mirror.command_dg_workspace_run),
    }


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help", "help"}:
        print(_general_help())
        return 0

    mode_token = args[0]
    rest = args[1:]
    if mode_token in ("--claude", "--codex"):
        # `--claude`/`--codex` are shorthand for `launch --engine <engine>`,
        # which can't be expressed as a plain LEGACY_ALIASES table lookup
        # since they also inject a default flag value. `--codex` matters now
        # that the default engine is claude -- it's the one-flag way back.
        engine = mode_token.lstrip("-")
        mode_token = "launch"
        rest = ["--engine", engine, *rest]

    mode = LEGACY_ALIASES.get(mode_token)
    if mode is None:
        print(f"Unknown command or flag: {mode_token}", file=sys.stderr)
        print()
        print(_general_help())
        return 2

    parser_factory, handler = _dispatch_table()[mode]
    parser = parser_factory(mode)
    parsed = parser.parse_args(rest)
    try:
        return int(handler(parsed))
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
