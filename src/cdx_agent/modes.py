"""Registry of every ``cdx-agent`` flag and the checks the bash front end delegates to it.

    python -m cdx_agent.modes --check-flag FLAG       exit 0 if known, else 2 with "unknown option ..."
    python -m cdx_agent.modes --supports-dry-run FLAG exit 0 if the mode honors --dry-run, else 3
    python -m cdx_agent.modes --help-text             help generated from this registry, grouped by category
"""
from __future__ import annotations

import difflib
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence


@dataclass(frozen=True)
class Mode:
    flag: str
    impl: str = "bash"                  # "python" | "bash"
    module: str = ""                    # cdx_agent.<module> for python modes
    supports_dry_run: bool = False
    writes: bool = False
    needs_network: bool = False
    help: str = ""
    category: str = "misc"
    deprecated: bool = False
    kind: str = "mode"                  # "mode" (selects what runs) | "option" (modifies a run)


def _m(flag, help, category, impl="bash", module="", dry=False, writes=False, net=False, dep=False, kind="mode"):
    return Mode(flag, impl, module, dry, writes, net, help, category, dep, kind)


def _o(flag, help):
    return _m(flag, help, "options", kind="option")


MODES: List[Mode] = [
    # diagnostics
    _m("--doctor", "Codex tool checks (deprecated)", "diagnostics", "python", "offload", dep=True),
    _m("--token-doctor", "token tool checks (deprecated)", "diagnostics", dep=True),
    _m("--session-doctor", "report session/runtime state for this repo", "diagnostics", "python", "cli"),
    _m("--workspace-doctor", "workspace graph checks", "diagnostics"),
    _m("--validate-skills", "validate skills", "skills"),
    _m("--skills-list", "list skills", "skills"),
    _m("--skills-audit", "audit skills against waivers", "skills"),
    # setup
    _m("--init-tools", "initialize local tools under codex_tools", "setup", writes=True),
    _m("--init-repo", "initialize repo layout", "setup", writes=True),
    _m("--init-workspace", "initialize a workspace graph", "setup", writes=True),
    _m("--install-token-tools", "install token tools", "setup", writes=True),
    _m("--install-hooks", "install hooks in the repo", "setup", "python", "cli", writes=True),
    # graph / context
    _m("--graph", "build the repo graph", "graph", writes=True),
    _m("--workspace-graph", "build the workspace graph", "graph", writes=True),
    _m("--detect-deps", "detect workspace dependencies", "graph"),
    _m("--review", "review the repo diff", "graph"),
    _m("--context-budget", "context budget report", "graph"),
    _m("--summarize-log", "summarize a log file", "graph"),
    _m("--compress-output", "run a command and compress its output (rest of argv)", "graph", writes=True),
    # usage
    _m("--usage", "daily usage (deprecated)", "usage", dep=True),
    _m("--usage-session", "session usage (deprecated)", "usage", dep=True),
    _m("--usage-month", "monthly usage (deprecated)", "usage", dep=True),
    _m("--agent-usage", "agent usage report", "usage", "python", "agent_usage"),
    # GrapeRoot / Dual-Graph (all deprecated)
    _m("--dg", "dual-graph for the repo (deprecated)", "dual-graph", dry=True, writes=True, dep=True),
    _m("--graperoot", "GrapeRoot for the repo (deprecated)", "dual-graph", dry=True, writes=True, dep=True),
    _m("--dg-root", "dual-graph for PATH (deprecated)", "dual-graph", dry=True, writes=True, dep=True),
    _m("--graperoot-root", "GrapeRoot for PATH (deprecated)", "dual-graph", dry=True, writes=True, dep=True),
    _m("--dg-workspace", "dual-graph workspace SPEC (deprecated)", "dual-graph", dry=True, writes=True, dep=True),
    _m("--dg-workspace-init", "create dual-graph workspace NAME PATH... (deprecated)", "dual-graph",
       dry=True, writes=True, dep=True),
    _m("--dg-workspace-list", "list dual-graph workspaces (deprecated)", "dual-graph", dep=True),
    _m("--dg-workspace-show", "show a dual-graph workspace (deprecated)", "dual-graph", dep=True),
    _m("--dg-workspace-clean", "clean a dual-graph workspace (deprecated)", "dual-graph", writes=True, dep=True),
    # cluster / jobs (rest of argv goes to the module)
    _m("--offload", "offload long work to a GPU host", "cluster", "python", "offload", writes=True, net=True),
    _m("--notify", "send a notification", "cluster", "python", "notify", net=True),
    _m("--move-verify", "copy, verify, then delete-by-user", "data", "python", "move_verify", writes=True),
    _m("--gpu-lock", "shared GPU hold/lock protocol", "cluster", "python", "gpu_lock", dry=True, writes=True),
    _m("--gpu-pick", "choose an eligible Slurm GPU node", "cluster", "python", "gpu_pick", net=True),
    _m("--cpu-job", "run or pick a node for a CPU job", "cluster", "python", "cpu_job", writes=True, net=True),
    _m("--watch", "watch a running job", "cluster", "python", "watch", net=True),
    _m("--handoff", "write a session handoff", "session", "python", "handoff", writes=True, net=True),
    _m("--brief", "session brief", "session", "python", "brief"),
    _m("--hash-repro", "re-run a command until its hash matches", "cluster"),
    _m("--teardown", "generate a teardown script from SPEC.json", "cluster", writes=True),
    # data
    _m("--coldbank", "cold bank and per-host caches", "data", "python", "coldbank", writes=True),
    _m("--hygiene", "repo/runtime hygiene check", "data", "python", "hygiene"),
    _m("--deploy", "deploy tools to the live root", "data", "python", "deploy", dry=True, writes=True),
    _m("--value", "value records", "data", "python", "values", writes=True),
    _m("--rm", "move files to trash instead of deleting", "data", "python", "trash", dry=True, writes=True),
    _m("--trash", "trash index, restore, drain", "data", "python", "trash", writes=True),
    _m("--inuse", "check whether paths are in use", "data", "python", "inuse"),
    _m("--suite", "per-file pytest fan-out with the test-routing map", "session", "python", "suite"),
    _m("--check", "check .cdx conventions, docs drift, report wording, refs", "session", "python", "check"),
    _m("--verify-log", "subagent claims and the owner's re-checks", "session", "python", "verify_log"),
    # site
    _m("--site-facts", "live GPU caps, free space and quota", "site", "python", "site_facts", net=True),
    # help
    _m("--help", "print help", "help"),
    _m("-h", "print help", "help"),
    # options
    _o("--token-saver", "compress command output"),
    _o("--no-token-saver", "do not compress command output"),
    _o("--cancel-active", "cancel the active session"),
    _o("--no-cancel-active", "never cancel the active session"),
    _o("--takeover", "take over the active session"),
    _o("--drain", "drain SECONDS before takeover (also --drain=SECONDS)"),
    _o("--safe", "workspace-write sandbox"),
    _o("--full", "full access (default)"),
    _o("--claude", "use the Claude engine (default)"),
    _o("--codex", "use the Codex engine"),
    _o("--secondary", "secondary session"),
    _o("--repo", "target repo PATH"),
    _o("--force-home-scan", "allow a scan of the home directory"),
    _o("--repo-local", "install into the repo"),
    _o("--runtime", "install into the runtime"),
    _o("--yes", "assume yes"),
    _o("--dry-run", "print what would be done (modes that support it)"),
]

BY_FLAG: Dict[str, Mode] = {m.flag: m for m in MODES}
CATEGORY_ORDER = ["data", "cluster", "session", "site", "diagnostics", "skills", "setup", "graph", "usage",
                  "dual-graph", "options", "help", "misc"]


def normalize(flag: str) -> str:
    """``--drain=5`` -> ``--drain``."""
    return flag.split("=", 1)[0]


def lookup(flag: str) -> Optional[Mode]:
    return BY_FLAG.get(normalize(flag))


def suggest(flag: str) -> Optional[str]:
    close = difflib.get_close_matches(normalize(flag), [m.flag for m in MODES if m.flag.startswith("--")], n=1, cutoff=0.5)
    return close[0] if close else None


def unknown_message(flag: str) -> str:
    near = suggest(flag)
    msg = f"unknown option {flag}"
    return f"{msg}; did you mean {near}?" if near else msg


def help_text() -> str:
    out = ["usage: cdx-agent [options] [--MODE ...] [-- codex/claude args]"]
    for cat in CATEGORY_ORDER:
        rows = [m for m in MODES if m.category == cat and m.flag != "-h"]
        if not rows:
            continue
        out.append("")
        out.append(f"{cat}:")
        for m in rows:
            notes = []
            if m.supports_dry_run:
                notes.append("dry-run")
            if m.deprecated:
                notes.append("deprecated")
            suffix = f" [{', '.join(notes)}]" if notes else ""
            out.append(f"  {m.flag:<24} {m.help}{suffix}")
    return "\n".join(out)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 2 and args[0] == "--check-flag":
        if lookup(args[1]) is not None:
            return 0
        print(unknown_message(args[1]), file=sys.stderr)
        return 2
    if len(args) == 2 and args[0] == "--supports-dry-run":
        mode = lookup(args[1])
        return 0 if mode is not None and mode.supports_dry_run else 3
    if args == ["--help-text"]:
        print(help_text())
        return 0
    print("usage: python -m cdx_agent.modes --check-flag FLAG | --supports-dry-run FLAG | --help-text", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
