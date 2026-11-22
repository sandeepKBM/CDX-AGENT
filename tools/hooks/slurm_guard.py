"""Raw sbatch/srun guard for the PreToolUse Bash hook (user ruling Q6, 2026-11-20).

Agents launch through the standard launchers (`cdx-agent --run`, `--gpu-lock`, `--cpu-job`, `--suite`), which add
placement, locks, thread pins, manifests and NFS-safe completion. A raw `sbatch`/`srun` in a Bash call is denied,
also inside `ssh HOST ...`, `bash -c`, `eval`, `timeout`/`env`/`sudo` wrappers. Read-only Slurm commands
(squeue, sinfo, sacct, scontrol show, sacctmgr show) are not affected.

Escape for a job no cdx-agent mode covers: put `CDX_RAW_SLURM=1` in the command (or the session env) and say why.
A hook is used instead of a permissions.deny rule because deny rules cannot carry an escape.
"""
from __future__ import annotations

import os
import re
from typing import Optional

import delete_guard

LAUNCHERS = {"sbatch", "srun", "salloc"}
HINT = ("raw Slurm launch blocked (user rule 2026-11-20): use `cdx-agent --run [--profile P | --cpus N --gpus K] -- CMD` "
        "(places the job as a step in your hold or a new job, with locks, thread pins and a manifest; "
        "`--plan` shows the choice), `cdx-agent --gpu-lock`, `--cpu-job` or `--suite`. If none fits, prefix "
        "`CDX_RAW_SLURM=1` and state why.")


def _launches(command: str, depth: int = 0) -> list[str]:
    found: list[str] = []
    if not re.search(r"\b(sbatch|srun|salloc)\b", command):
        return found
    segs = delete_guard._segments(command) or []
    for words in segs:
        head, args = delete_guard._head(words)
        if head in LAUNCHERS:
            found.append(head)
        elif head in {"ssh", "pdsh"} and depth < 2:
            remote = _ssh_remote(args)
            if remote:
                found += _launches(remote, depth + 1)
        elif head in {"bash", "sh", "zsh"} and "-c" in args and depth < 2:
            i = args.index("-c")
            if i + 1 < len(args):
                found += _launches(args[i + 1], depth + 1)
        elif head == "eval" and depth < 2:
            found += _launches(" ".join(args), depth + 1)
    return found


SSH_VALUE_OPTS = {"-o", "-p", "-i", "-l", "-J", "-F", "-b", "-c", "-D", "-E", "-L", "-R", "-S", "-W", "-w"}


def _ssh_remote(args: list[str]) -> str:
    """The remote command of `ssh [opts] HOST CMD...` (empty when none)."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in SSH_VALUE_OPTS else 1
    return " ".join(args[i + 1:])


def check(command: str) -> Optional[str]:
    """Deny reason, or None."""
    if os.environ.get("CDX_RAW_SLURM") == "1" or re.search(r"(^|[\s;&|(])CDX_RAW_SLURM=1\b", command):
        return None
    hits = _launches(command)
    if not hits:
        return None
    return f"cdx-agent: {', '.join(sorted(set(hits)))}: {HINT}"
