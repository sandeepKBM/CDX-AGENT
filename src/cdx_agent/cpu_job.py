"""Run CPU-heavy work (tests, offline replays, data generation) on an idle ilab node instead of a loaded workstation.

    python -m cdx_agent.cpu_job [--node N] [--threads 8] [--mem 16G] [--time 2:00:00] [--name NAME] [--log FILE]
                                [--wait] [--timeout S] [--cwd DIR] [--dry-run] -- CMD ...

- Picks the node with the most idle CPUs (``sinfo``) among ``--prefix`` (default ilab only; user rule) unless ``--node`` is given.
- Sets OMP/OPENBLAS/MKL/NUMEXPR/TORCH thread limits to ``--threads`` (Slurm jobs here see every core of the node and rejected
  ``-c``/``--cpus-per-task``: thread counts are the caller's responsibility; ``ulimit -u`` 2000 is per user per node).
- Default: ``sbatch`` (no GPU), prints ``job=<id> node=<n> log=<file>`` and returns at once (no waiting in the caller's turn).
  ``--wait``: ``srun`` in the foreground with ``--timeout`` (default 3600 s) and the command's exit status (e.g. a test suite).
- The command runs in ``--cwd`` (default: the current directory; must be on shared storage, e.g. /common/users).
- Data-locality guard: Slurm nodes cannot read workstation-local disks. Before submitting, the repo's top-level symlinks (data,
  checkpoints, ...), ``external/*`` symlinks and every path in the command are resolved; if one lands under an
  ``--unreadable-prefix`` (default /data/local, env CDX_NODE_UNREADABLE, comma separated) the job is refused (exit 2) and the exact
  local background command is printed (niced, OMP/MKL/OPENBLAS=2, CUDA_VISIBLE_DEVICES=, timeout). ``--allow-local-data`` overrides.
Exit: 0 submitted / command ok, 1 command failed, 2 bad arguments, no node, or data not readable from nodes.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Optional, Sequence

from cdx_agent.gpu_pick import _state_blocked, parse_mem_mb, parse_scontrol

FOOTPRINTS = "/common/users/ss5772/codex_tools/base/footprints.json"
DEFAULT_UNREADABLE = ("/data/local",)
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


def idle_cpus(sinfo_text: str) -> dict:
    out = {}
    for line in sinfo_text.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            try:
                out[parts[0]] = int(parts[1].split("/")[1])
            except (IndexError, ValueError):
                pass
    return out


def node_rows(sinfo_text: str, scontrol_text: str, prefixes: Sequence[str], need_cpus: int, mem_mb: int, nodes: Optional[Sequence[str]] = None):
    """-> [(node, idle_cpus, free_mb|None, state, problem|None)] ranked best first (qualifying nodes first, by idle CPUs).

    Free memory is RealMemory - AllocMem (gpu_pick.parse_scontrol); a node qualifies only if free mem >= mem_mb, idle CPUs >= need_cpus
    and its state accepts jobs (not drain/down/maint/reserved...).
    """
    idle, info, rows = idle_cpus(sinfo_text), parse_scontrol(scontrol_text), []
    for name in (nodes if nodes is not None else idle):
        if nodes is None and not any(name.startswith(p) for p in prefixes):
            continue
        i, n = idle.get(name), info.get(name)
        free = n.free_mb if n else None
        state = n.state if n else "?"
        why = []
        if i is None or i < need_cpus:
            why.append(f"idle CPUs {i} < {need_cpus}")
        if free is None:
            why.append("free mem unknown")
        elif free < mem_mb:
            why.append(f"free mem {free / 1024:.0f}G < {mem_mb / 1024:.0f}G")
        if n is None or _state_blocked(state):
            why.append(f"state {state}")
        rows.append((name, i if i is not None else -1, free, state, "; ".join(why) or None))
    rows.sort(key=lambda r: (r[4] is not None, -r[1], -(r[2] or 0)))
    return rows


def fmt_row(r) -> str:
    free = "?" if r[2] is None else f"{r[2] / 1024:.0f}G"
    return f"{r[0]}: free mem {free}, idle CPUs {r[1]}, state {r[3]}" + (f" [{r[4]}]" if r[4] else "")


def profile_mem(name: str, procs: int, path: str = FOOTPRINTS) -> str:
    """Default --mem (e.g. ``4G``) from the footprint table: max(peak_gb, per_proc_gb * procs), rounded up."""
    with open(path) as f:
        prof = json.load(f)["profiles"][name]
    vals = [float(prof.get("peak_gb") or 0)]
    if prof.get("per_proc_gb") is not None:
        vals.append(float(prof["per_proc_gb"]) * procs)
    return f"{max(1, math.ceil(max(vals)))}G"


def ensure_footprints(path: str = FOOTPRINTS) -> None:
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump({"profiles": {"mujoco-mpc-sweep": {"steady_gb": 0.5, "peak_gb": 0.5, "per_proc_gb": 0.5}}}, f, indent=2)
            f.write("\n")


def _fetch(host: Optional[str]) -> tuple:
    sinfo = subprocess.run(_slurm("sinfo -N -O NodeList,CPUsState -h", host), capture_output=True, text=True, timeout=60).stdout
    scon = subprocess.run(_slurm("scontrol show node -o", host), capture_output=True, text=True, timeout=60).stdout
    return sinfo, scon


def _under(path: str, prefixes: Sequence[str]) -> Optional[str]:
    for prefix in prefixes:
        prefix = prefix.rstrip("/") or "/"
        if path == prefix or path.startswith(prefix.rstrip("/") + "/"):
            return prefix
    return None


def _repo_root(cwd: str) -> str:
    cur = Path(os.path.abspath(cwd))
    for d in (cur, *cur.parents):
        if (d / ".git").exists():
            return str(d)
    return cur


def find_unreadable(cmd: Sequence[str], cwd: str, prefixes: Sequence[str] = DEFAULT_UNREADABLE) -> list[tuple[str, str]]:
    """Return ``[(what, resolved)]`` for everything the job would touch that resolves under a node-unreadable prefix.

    Looks at the repo root's top-level symlinks, symlinks directly under ``external/``, and every word of the command that is
    (or resolves relative to ``cwd`` to) an existing path or an absolute path.
    """
    prefixes = [p for p in prefixes if p]
    if not prefixes:
        return []
    found: list[tuple[str, str]] = []
    seen: set[str] = set()

    def check(label: str, path: str) -> None:
        try:
            real = os.path.realpath(path)
        except (OSError, ValueError):
            return
        if _under(real, prefixes) and label not in seen:
            seen.add(label)
            found.append((label, real))

    root = _repo_root(cwd)
    try:
        for entry in sorted(os.scandir(root), key=lambda e: e.name):
            if entry.is_symlink():
                check(entry.name, entry.path)
            elif entry.name == "external" and entry.is_dir():
                for sub in sorted(os.scandir(entry.path), key=lambda e: e.name):
                    if sub.is_symlink():
                        check(f"external/{sub.name}", sub.path)
    except OSError:
        pass
    words: list[str] = []
    for word in cmd:       # a quoted shell string (bash -c '...') is split further so its paths are seen
        try:
            words.extend(shlex.split(word) if (" " in word or "\n" in word) else [word])
        except ValueError:
            words.append(word)
    for word in words:
        for cand in {word, word.split("=", 1)[-1], word.split("::", 1)[0]}:
            if not cand or cand.startswith("-") and "/" not in cand:
                continue
            full = cand if os.path.isabs(cand) else os.path.join(cwd, cand)
            if "/" in cand or os.path.lexists(full):
                check(cand, full)
    return found


def local_command(cmd: Sequence[str], cwd: str, name: str = "cpujob", timeout: int = 3600, threads: int = 2) -> str:
    """The exact command to run a CPU job on this machine in the background without hurting other users."""
    env = " ".join(f"{v}={threads}" for v in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"))
    log = os.path.join(cwd, "logs", "cpu_jobs", f"local_{name}.log")
    return (f"mkdir -p {shlex.quote(os.path.dirname(log))} && cd {shlex.quote(cwd)} && {env} CUDA_VISIBLE_DEVICES= "
            f"nohup nice -n 10 timeout {timeout} {shlex.join(cmd)} > {shlex.quote(log)} 2>&1 &")


def per_proc_threads(threads: int, procs: int) -> int:
    """OMP/MKL/torch threads for EACH of ``procs`` worker processes, so the job uses about ``threads`` CPUs in total."""
    return max(1, int(threads) // max(1, int(procs)))


def build_script(cmd: Sequence[str], cwd: str, threads: int) -> str:
    env = " ".join(f"{v}={threads}" for v in THREAD_VARS)
    return f"cd {shlex.quote(cwd)} && export {env} PYTHONUNBUFFERED=1 && {shlex.join(cmd)}"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m cdx_agent.cpu_job", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--node", default=None)
    p.add_argument("--prefix", default="ilab")   # user 2026-10-17: ilab only (rlab nodes carry the GPU jobs)
    p.add_argument("--threads", type=int, default=8)
    p.add_argument("--mem", default=None, help="job memory (default 16G, or from --profile)")
    p.add_argument("--profile", default=None, help="memory profile from the footprint table (%s)" % FOOTPRINTS)
    p.add_argument("--procs", type=int, default=1, help="worker processes: memory = per_proc_gb x N (--profile); thread vars = --threads // N per process")
    p.add_argument("--force", action="store_true", help="with --node: submit even if the node fails the memory/CPU/state check")
    p.add_argument("--time", default="2:00:00")
    p.add_argument("--name", default="cpujob")
    p.add_argument("--log", default=None, help="sbatch log (default: <cwd>/logs/cpu_jobs/<name>_%%j.log)")
    p.add_argument("--wait", action="store_true", help="srun in the foreground (with --timeout) instead of sbatch")
    p.add_argument("--timeout", type=int, default=3600)
    p.add_argument("--cwd", default=None)
    p.add_argument("--slurm-host", default=None)
    p.add_argument("--unreadable-prefix", default=os.environ.get("CDX_NODE_UNREADABLE", ",".join(DEFAULT_UNREADABLE)),
                   help="comma list of path prefixes Slurm nodes cannot read (workstation-local disks)")
    p.add_argument("--allow-local-data", action="store_true", help="submit even if data resolves under an unreadable prefix")
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
    bad = [] if a.allow_local_data else find_unreadable(cmd, cwd, [x.strip() for x in a.unreadable_prefix.split(",") if x.strip()])
    if bad:
        print("cpu_job: refused: Slurm nodes cannot read workstation-local data:", file=sys.stderr)
        for label, real in bad:
            print(f"  {label} -> {real}", file=sys.stderr)
        print("Run it on this machine instead (background, niced, threads capped; do not wait on it):\n  "
              + local_command(cmd, cwd, a.name, a.timeout), file=sys.stderr)
        print("(override: --allow-local-data)", file=sys.stderr)
        return 2
    mem = a.mem
    if a.profile:
        ensure_footprints()
        try:
            pm = profile_mem(a.profile, a.procs)
        except (OSError, KeyError, ValueError) as e:
            print(f"cpu_job: profile {a.profile!r} not usable ({e!r}) in {FOOTPRINTS}", file=sys.stderr)
            return 2
        mem = mem or pm
    mem = mem or "16G"
    a.mem = mem
    mem_mb = parse_mem_mb(mem)
    prefixes = [s.strip() for s in a.prefix.split(",") if s.strip()]
    sinfo, scon = _fetch(a.slurm_host)
    node, why = a.node, None
    if node is None:
        rows = node_rows(sinfo, scon, prefixes, a.threads, mem_mb)
        ok = [r for r in rows if r[4] is None]
        if not ok:
            print(f"cpu_job: no {a.prefix} node has >= {a.threads} idle CPUs, >= {mem} free memory and an accepting state. Best 3:", file=sys.stderr)
            for r in rows[:3]:
                print("  " + fmt_row(r), file=sys.stderr)
            return 2
        node, why = ok[0][0], fmt_row(ok[0])
    else:
        r = node_rows(sinfo, scon, prefixes, a.threads, mem_mb, nodes=[node])[0]
        why = fmt_row(r)
        if r[4] and not a.force:
            print(f"cpu_job: refused --node {node}: {r[4]} ({why}); add --force to submit anyway", file=sys.stderr)
            return 2
    script = build_script(cmd, cwd, per_proc_threads(a.threads, a.procs))   # --procs N: threads per process = threads // N
    if a.wait:
        remote = f"timeout {a.timeout} srun -w {node} --mem={a.mem} -t {a.time} -J {shlex.quote(a.name)} bash -c {shlex.quote(script)}"
    else:
        log = a.log or os.path.join(cwd, "logs", "cpu_jobs", f"{a.name}_%j.log")
        os.makedirs(os.path.dirname(log), exist_ok=True)
        remote = (f"sbatch --parsable -w {node} --mem={a.mem} -t {a.time} -J {shlex.quote(a.name)} -o {shlex.quote(log)} "
                  f"--wrap {shlex.quote(script)}")
    if a.dry_run:
        print(f"cpu_job: node={node} mem={mem}: {why}", file=sys.stderr)
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
