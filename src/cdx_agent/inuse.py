"""In-use probe: is anything still using PATH?

    python -m cdx_agent.inuse PATH [--hosts h1,h2] [--json] [--local-only]

Verdicts: "free", "in_use", "unknown".  Callers MUST treat "unknown" as in use.

Sources: (1) local /proc scan (cmdline, cwd, fds); (2) squeue jobs of $USER whose WorkDir or
Command is under PATH (run locally when `squeue` is on PATH, else via ssh to $CDX_SLURM_HOST like
gpu_pick); (3) for paths on a network filesystem only, the same /proc scan sent over ssh stdin to
each host (default: $CDX_WORKSTATIONS plus nodes of the user's running jobs).  An unreachable host,
or an unavailable squeue on a network path, gives "unknown".  `local_only=True` does only (1).
Not yet covered: running job manifests and coldbank leases (placeholders until they exist).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional, Sequence, Set

NETWORK_FSTYPES = {"nfs", "nfs4", "lustre", "cifs"}

# Runs on any host (also executed locally via exec).  Expects ROOT and optional PROC to be defined first.
PROBE_SRC = r'''
import os
def _scan(root, proc_root="/proc", me=None):
    root = os.path.realpath(root)
    def inside(t):
        return t == root or t.startswith(root.rstrip("/") + "/")
    out = []
    try:
        pids = [int(p) for p in os.listdir(proc_root) if p.isdigit()]
    except OSError:
        return out
    for pid in pids:
        if pid == me:
            continue
        base = os.path.join(proc_root, str(pid))
        try:
            with open(os.path.join(base, "comm")) as fh:
                comm = fh.read().strip()
        except OSError:
            comm = "?"
        try:
            with open(os.path.join(base, "cmdline"), "rb") as fh:
                args = fh.read().decode("utf-8", "replace").split("\0")
        except OSError:
            args = []
        hit = None
        for a in args:
            if a.startswith("/") and inside(os.path.normpath(a)):
                hit = "cmdline " + a
                break
        if hit is None:
            links = [os.path.join(base, "cwd")]
            try:
                links += [os.path.join(base, "fd", f) for f in os.listdir(os.path.join(base, "fd"))]
            except OSError:
                pass
            for link in links:
                try:
                    t = os.readlink(link)
                except OSError:
                    continue
                if t.endswith(" (deleted)"):
                    t = t[:-10]
                if inside(t):
                    hit = ("cwd " if link.endswith("/cwd") else "fd ") + t
                    break
        if hit:
            out.append("pid %d %s: %s" % (pid, comm, hit))
    return out
'''
_PROBE_MAIN = "\nfor _l in _scan(ROOT):\n    print(_l)\n"


def _local_scan(path: str, proc_root: str) -> List[str]:
    ns: Dict[str, Any] = {}
    exec(PROBE_SRC, ns)  # noqa: S102 - our own constant
    return list(ns["_scan"](path, proc_root, os.getpid()))


def _fstype(path: str, mounts_file: str) -> str:
    real = os.path.realpath(path)
    best, fst = "", ""
    try:
        with open(mounts_file) as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mp = parts[1].replace("\\040", " ")
                if (real == mp or real.startswith(mp.rstrip("/") + "/")) and len(mp) >= len(best):
                    best, fst = mp, parts[2]
    except OSError:
        return ""
    return fst


def _under(target: str, root: str) -> bool:
    target = os.path.normpath(target)
    return target == root or target.startswith(root.rstrip("/") + "/")


def _squeue(run: Callable[..., Any], user: str) -> Optional[str]:
    cmd = f'squeue -u {user} -h -t RUNNING -o "%i|%N|%Z|%o"'
    if shutil.which("squeue"):
        argv = ["squeue", "-u", user, "-h", "-t", "RUNNING", "-o", "%i|%N|%Z|%o"]
    else:
        host = os.environ.get("CDX_SLURM_HOST", "rlab1")
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, cmd]
    try:
        r = run(argv, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def _job_nodes(field: str) -> Set[str]:
    from .gpu_pick import _expand_nodes

    return set(_expand_nodes(field)) if field.strip() else set()


def in_use(path: Any, hosts: Optional[Sequence[str]] = None, *, proc_root: str = "/proc",
           run: Callable[..., Any] = subprocess.run, mounts_file: str = "/proc/mounts",
           local_only: bool = False) -> Dict[str, Any]:
    path = os.path.realpath(str(path))
    evidence = _local_scan(path, proc_root)
    unknown = False
    if local_only:
        return {"verdict": "in_use" if evidence else "free", "evidence": evidence}
    network = _fstype(path, mounts_file) in NETWORK_FSTYPES
    user = os.environ.get("USER", "")
    nodes: Set[str] = set()
    out = _squeue(run, user)
    if out is None:
        if network:
            unknown = True
            evidence.append("squeue unavailable")
    else:
        for line in out.splitlines():
            parts = line.split("|", 3)
            if len(parts) < 4:
                continue
            jid, node, wdir, cmd = (p.strip() for p in parts)
            nodes |= _job_nodes(node)
            if _under(wdir, path) or any(_under(tok, path) for tok in cmd.split() if tok.startswith("/")):
                evidence.append(f"squeue job {jid}: WorkDir={wdir} Command={cmd}")
    if network:
        if hosts is None:
            ws = [h.strip() for h in os.environ.get("CDX_WORKSTATIONS", "").split(",") if h.strip()]
            hosts = sorted(set(ws) | nodes)
        me = socket.gethostname().split(".")[0]
        for host in hosts:
            if host.split(".")[0] == me:
                continue
            script = f"ROOT = {path!r}\n" + PROBE_SRC + _PROBE_MAIN
            argv = ["ssh", "-o", "BatchMode=yes", "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=5",
                    host, "python3", "-"]
            try:
                r = run(argv, input=script, capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired) as exc:
                unknown = True
                evidence.append(f"{host}: unreachable ({exc})")
                continue
            if r.returncode != 0:
                unknown = True
                evidence.append(f"{host}: probe failed (exit {r.returncode})")
                continue
            evidence += [f"{host}: {ln}" for ln in r.stdout.splitlines() if ln.strip()]
    hit = any(not (e.startswith("squeue unavailable") or ": unreachable" in e or ": probe failed" in e)
              for e in evidence)
    verdict = "in_use" if hit else ("unknown" if unknown else "free")
    return {"verdict": verdict, "evidence": evidence}


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m cdx_agent.inuse")
    ap.add_argument("path")
    ap.add_argument("--hosts", default=None, help="comma list; default $CDX_WORKSTATIONS + job nodes")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--local-only", action="store_true")
    a = ap.parse_args(argv)
    hosts = [h for h in a.hosts.split(",") if h] if a.hosts is not None else None
    res = in_use(a.path, hosts, local_only=a.local_only)
    if a.json:
        print(json.dumps(res))
    else:
        print(res["verdict"])
        for e in res["evidence"]:
            print("  " + e)
    return 0 if res["verdict"] == "free" else 1


if __name__ == "__main__":
    sys.exit(main())
