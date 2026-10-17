"""Run CPU-heavy work (tests, offline replays, data generation) on an idle ilab node instead of a loaded workstation.

    python -m cdx_agent.cpu_job [--node N] [--threads 8] [--mem 16G] [--time 2:00:00] [--name NAME] [--log FILE]
                                [--wait] [--timeout S] [--cwd DIR] [--dry-run] -- CMD ...

- Picks the node with the most idle CPUs (``sinfo``) among ``--prefix`` (default ilab only; user rule) unless ``--node`` is given.
- Sets OMP/OPENBLAS/MKL/NUMEXPR/TORCH thread limits to ``--threads`` (Slurm jobs here see every core of the node and rejected
  ``-c``/``--cpus-per-task``: thread counts are the caller's responsibility; ``ulimit -u`` 2000 is per user per node).
- Default: ``sbatch`` (no GPU), prints ``job=<id> node=<n> log=<file>`` and returns at once (no waiting in the caller's turn).
  ``--wait``: ``srun`` in the foreground with ``--timeout`` (default 3600 s) and the command's exit status (e.g. a test suite).
- The command runs in ``--cwd`` (default: the current directory; must be on shared storage, e.g. /common/users).
Exit: 0 submitted / command ok, 1 command failed, 2 bad arguments or no node.
"""
from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from typing import Optional, Sequence

THREAD_VARS = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS", "TORCH_NUM_THREADS")


def _slurm(cmd: str, host: Optional[str]) -> list[str]:
    host = host or os.environ.get("CDX_SLURM_HOST", "rlab1")
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, cmd]


def pick_node(sinfo_text: str, prefixes: Sequence[str], need: int) -> Optional[str]:
    """``sinfo -N -O NodeList,CPUsState -h`` text (``node A/I/O/T``) -> the node with the most idle CPUs (>= need) among prefixes."""
    best, best_idle = None, -1
    for line in sinfo_text.splitlines():
        parts = line.split()
        if len(parts) < 2 or not any(parts[0].startswith(p) for p in prefixes):
            continue
        try:
            idle = int(parts[1].split("/")[1])
        except (IndexError, ValueError):
            continue
        if idle >= need and idle > best_idle:
            best, best_idle = parts[0], idle
    return best


def build_script(cmd: Sequence[str], cwd: str, threads: int) -> str:
    env = " ".join(f"{v}={threads}" for v in THREAD_VARS)
    return f"cd {shlex.quote(cwd)} && export {env} PYTHONUNBUFFERED=1 && {shlex.join(cmd)}"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m cdx_agent.cpu_job", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--node", default=None)
    p.add_argument("--prefix", default="ilab")   # user 2026-10-17: ilab only (rlab nodes carry the GPU jobs)
    p.add_argument("--threads", type=int, default=8)
    p.add_argument("--mem", default="16G")
    p.add_argument("--time", default="2:00:00")
    p.add_argument("--name", default="cpujob")
    p.add_argument("--log", default=None, help="sbatch log (default: <cwd>/logs/cpu_jobs/<name>_%%j.log)")
    p.add_argument("--wait", action="store_true", help="srun in the foreground (with --timeout) instead of sbatch")
    p.add_argument("--timeout", type=int, default=3600)
    p.add_argument("--cwd", default=None)
    p.add_argument("--slurm-host", default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("cmd", nargs=argparse.REMAINDER)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    a = build_parser().parse_args(argv)
    cmd = list(a.cmd)
    if cmd[:1] == ["--"]:
        cmd = cmd[1:]
    if not cmd:
        print("cpu_job: no command (use: -- CMD ...)", file=sys.stderr)
        return 2
    cwd = os.path.abspath(a.cwd or os.getcwd())
    node = a.node
    if node is None:
        q = subprocess.run(_slurm("sinfo -N -O NodeList,CPUsState -h", a.slurm_host), capture_output=True, text=True, timeout=60)
        node = pick_node(q.stdout, [s.strip() for s in a.prefix.split(",") if s.strip()], need=a.threads)
        if node is None:
            print(f"cpu_job: no {a.prefix} node with >= {a.threads} idle CPUs", file=sys.stderr)
            return 2
    script = build_script(cmd, cwd, a.threads)
    if a.wait:
        remote = f"timeout {a.timeout} srun -w {node} --mem={a.mem} -t {a.time} -J {shlex.quote(a.name)} bash -c {shlex.quote(script)}"
    else:
        log = a.log or os.path.join(cwd, "logs", "cpu_jobs", f"{a.name}_%j.log")
        os.makedirs(os.path.dirname(log), exist_ok=True)
        remote = (f"sbatch --parsable -w {node} --mem={a.mem} -t {a.time} -J {shlex.quote(a.name)} -o {shlex.quote(log)} "
                  f"--wrap {shlex.quote(script)}")
    if a.dry_run:
        print(remote)
        return 0
    r = subprocess.run(_slurm(remote, a.slurm_host), text=True, capture_output=not a.wait)
    if a.wait:
        return 0 if r.returncode == 0 else 1
    if r.returncode != 0:
        print(r.stderr.strip()[-500:], file=sys.stderr)
        return 1
    job = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "?"
    print(f"job={job} node={node} log={log.replace('%j', job)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
