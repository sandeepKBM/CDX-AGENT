from __future__ import annotations

import argparse
import sys
from typing import Callable

from . import graph
from . import token_tools


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
}


def _general_help() -> str:
    return (
        "CDX-AGENT\n"
        "A clean, cross-platform packaging of the repository graph and token-saving helpers.\n\n"
        "Usage:\n"
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
    }


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help", "help"}:
        print(_general_help())
        return 0

    mode_token = args[0]
    mode = LEGACY_ALIASES.get(mode_token)
    if mode is None:
        print(f"Unknown command or flag: {mode_token}", file=sys.stderr)
        print()
        print(_general_help())
        return 2

    parser_factory, handler = _dispatch_table()[mode]
    parser = parser_factory(mode)
    parsed = parser.parse_args(args[1:])
    try:
        return int(handler(parsed))
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
