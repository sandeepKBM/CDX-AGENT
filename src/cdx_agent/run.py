"""One launcher for every placement: a step in your Slurm hold, a new Slurm job, a westeros GPU, or this machine.

    python -m cdx_agent.run [--profile NAME | --cpus N --gpus K --gpu-type T --gpu-mem G --mem M --disk D --time T
                             --host-class C[,C]] [--data PATH]... [--snapshot] [--detach] [--name N]
                             [--plan] [--dry-run]
                            -- CMD ...
    python -m cdx_agent.run --collect RUN_DIR [--timeout S]

Spec file <repo>/.cdx/resources.yaml (falls back to the main checkout's through ``git rev-parse --git-common-dir``):
``profiles`` (cpus, gpus, gpu_type, gpu_mem, mem, time, threads_per_proc, host_class, footprint, snapshot, env),
``data``
({path: {tier, readable_from}}) and ``scratch`` ({ENV_VAR: subdir}, a per-job directory on node-local disk).
Optional <repo>/.cdx/site.json overrides SITE_DEFAULTS (westeros GPU split, lock dir, scratch roots, node prefixes).

resolve() order (host_class narrows and orders the places; default slurm-step, slurm-job, westeros, local):
  1. places that cannot read the data are dropped (cpu_job.find_unreadable + declared ``readable_from``);
  2. slurm-step: the user has a RUNNING job and the account node cap is 1 -> srun --jobid=HOLD --overlap (zero
     GPUs allowed; with GPUs the step takes gpu_lock locks);
  3. slurm-job: a new job (gpu_pick.evaluate for GPUs, cpu_job.node_rows for CPUs; sbatch, or srun in the foreground);
  4. westeros: GPUs through gpu_lock node-run on this machine (only when this host is westeros);
  5. local: idle cores (nproc - loadavg1) cover the request.
``--plan`` prints ``PLAN ...`` plus one ``rejected PLACE: reason`` line per rejected place and runs nothing;
``--dry-run``
prints the wrapped script and the exact ssh/srun/sbatch command and runs nothing.

Every run gets <repo>/.cdx/runs/<job_id>/ (NFS): job.sh, MANIFEST.json, and with --detach run.log; the wrapped script
writes DONE.json (temp name, then rename) when CMD ends. ``collect`` reads it through nfs.wait_visible and rewrites the
manifest (finished, exit_code, state). Foreground runs pass CMD's exit code through; --detach prints the run dir
and returns.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from cdx_agent import cpu_job, gpu_lock, manifest, nfs
from cdx_agent import gpu_pick as gp

CLASSES = ("slurm-step", "slurm-job", "westeros", "local")
SITE_DEFAULTS: dict[str, Any] = {
    "westeros_gpus": "0-3",
    "lock_dir": gpu_lock.DEFAULT_LOCK_DIR,
    "node_scratch_root": "/freespace/local/$USER/jobs",
    "local_scratch_root": "/tmp/$USER/jobs",
    "node_snapshot_root": "/freespace/local/$USER/snapshots",
    "local_snapshot_root": "/tmp/$USER/snapshots",
    "node_prefixes": ["ilab"],
    "gpu_prefixes": ["rlab", "ilab"],
    "python": "python3",
}
EXIT_REFUSED = 2


# --------------------------------------------------------------------------- spec / site
@dataclass
class Spec:
    profiles: dict[str, dict[str, Any]] = field(default_factory=dict)
    data: dict[str, dict[str, Any]] = field(default_factory=dict)
    scratch: dict[str, str] = field(default_factory=dict)
    path: Optional[str] = None


def _git(args: Sequence[str], cwd: str) -> Optional[str]:
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def repo_root(cwd: str) -> str:
    top = _git(["rev-parse", "--show-toplevel"], cwd)
    return top.strip() if top and top.strip() else os.path.abspath(cwd)


def cdx_file(repo: str, name: str) -> Optional[str]:
    """<repo>/.cdx/NAME, else the main checkout's (via --git-common-dir, so worktrees see it); None if neither."""
    own = os.path.join(repo, ".cdx", name)
    if os.path.isfile(own):
        return own
    common = _git(["rev-parse", "--git-common-dir"], repo)
    if common and common.strip():
        common_dir = os.path.normpath(os.path.join(repo, common.strip()))
        main = os.path.join(os.path.dirname(common_dir), ".cdx", name)
        if os.path.isfile(main):
            return main
    return None


def load_spec(repo: str) -> Spec:
    path = cdx_file(repo, "resources.yaml")
    if path is None:
        return Spec()
    import yaml
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return Spec(dict(raw.get("profiles") or {}), dict(raw.get("data") or {}), dict(raw.get("scratch") or {}), path)


def load_site(repo: str) -> dict[str, Any]:
    site = dict(SITE_DEFAULTS)
    path = cdx_file(repo, "site.json")
    if path:
        with open(path, encoding="utf-8") as fh:
            site.update(json.load(fh))
    return site


# --------------------------------------------------------------------------- request / live state
@dataclass
class Request:
    cmd: list[str]
    cwd: str
    cpus: int = 1
    gpus: int = 0
    gpu_type: Optional[str] = None
    gpu_mem: Optional[str] = None
    mem: Optional[str] = None
    disk: Optional[str] = None
    time: str = "2h"
    host_class: list[str] = field(default_factory=list)
    threads_per_proc: int = 1
    env: dict[str, str] = field(default_factory=dict)
    scratch: dict[str, str] = field(default_factory=dict)
    footprint: Optional[str] = None
    snapshot: bool = False
    data: list[str] = field(default_factory=list)
    name: str = "run"


@dataclass
class Live:
    user: str = ""
    host: Optional[str] = None
    gpu_cap: int = 8
    node_cap: int = 1
    squeue_text: str = ""
    queue: gp.QueueStats = field(default_factory=lambda: gp.QueueStats(0, {}, []))
    sinfo_gpu: str = ""
    sinfo_cpu: str = ""
    scontrol: Optional[str] = None
    others: Optional[str] = None
    cores: int = 1
    load1: float = 0.0
    hostname: str = ""


@dataclass
class Placement:
    cls: str
    cpus: int
    gpus: int
    mem: str
    time: str
    node: Optional[str] = None
    hold: Optional[str] = None
    gres: Optional[str] = None
    gpu_type: Optional[str] = None
    rejected: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class Rejections:
    rejected: list[tuple[str, str]]
    notes: list[str] = field(default_factory=list)


def parse_time_min(text: str) -> int:
    """``2h`` / ``90m`` / ``1:30:00`` / ``45`` (minutes) / ``2-00:00:00`` -> minutes."""
    t = str(text).strip().lower()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([hms]?)", t)
    if m:
        v, u = float(m.group(1)), m.group(2) or "m"
        return max(1, int(round(v * {"h": 60, "m": 1, "s": 1 / 60}[u])))
    m = re.fullmatch(r"(?:(\d+)-)?(\d+):(\d+)(?::(\d+))?", t)
    if not m:
        raise ValueError(f"bad time {text!r} (use 2h, 90m or H:MM:SS)")
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return max(1, d * 1440 + h * 60 + mi + (1 if s else 0))


def build_request(args: argparse.Namespace, spec: Spec, cmd: list[str], cwd: str) -> Request:
    prof: dict[str, Any] = {}
    if args.profile:
        if args.profile not in spec.profiles:
            raise KeyError(f"profile {args.profile!r} not in {spec.path or '.cdx/resources.yaml (not found)'}")
        prof = dict(spec.profiles[args.profile] or {})

    def pick(cli: Any, key: str, default: Any) -> Any:
        return cli if cli is not None else prof.get(key, default)

    hc = args.host_class if args.host_class else prof.get("host_class") or []
    if isinstance(hc, str):
        hc = [x.strip() for x in hc.split(",") if x.strip()]
    words = " ".join(cmd)
    data = list(args.data or []) + [p for p in spec.data if p in words and p not in (args.data or [])]
    return Request(
        cmd=cmd, cwd=cwd, cpus=int(pick(args.cpus, "cpus", 1)), gpus=int(pick(args.gpus, "gpus", 0)),
        gpu_type=pick(args.gpu_type, "gpu_type", None), gpu_mem=pick(args.gpu_mem, "gpu_mem", None),
        mem=pick(args.mem, "mem", None), disk=pick(args.disk, "disk", None), time=str(pick(args.time, "time", "2h")),
        host_class=list(hc), threads_per_proc=int(prof.get("threads_per_proc", 1)),
        env={str(k): str(v) for k, v in (prof.get("env") or {}).items()},
        scratch={str(k): str(v) for k, v in spec.scratch.items()},
        footprint=prof.get("footprint"), snapshot=bool(args.snapshot or prof.get("snapshot", False)),
        data=data, name=args.name or re.sub(r"[^A-Za-z0-9_.-]+", "-", os.path.basename(cmd[0]))[:40] or "run",
    )


def local_cores_and_load() -> tuple[int, float]:
    try:
        load = os.getloadavg()[0]
    except OSError:
        load = 0.0
    return os.cpu_count() or 1, load


def fetch_live(req: Request, user: str, host: Optional[str]) -> Live:
    """Read-only Slurm queries over ssh (the same helpers gpu_pick / cpu_job use)."""
    live = Live(user=user, host=host, hostname=socket.gethostname())
    live.gpu_cap, live.node_cap = gp.read_grptres(user, host)
    rc, out, _ = gp._output(gp.run_command(gp._slurm(f'squeue -u {shlex.quote(user)} -h -o "%i|%j|%N|%b|%T|%r"', host)))
    live.squeue_text = out if rc == 0 else ""
    live.queue = gp.parse_squeue(live.squeue_text)
    cmd_scon = gp._output(gp.run_command(gp._slurm("scontrol show node -o", host)))
    live.scontrol = cmd_scon[1] if cmd_scon[0] == 0 else None
    if req.gpus > 0:
        gpu_sinfo = "sinfo -N -O NodeList:30,Gres:60,GresUsed:80,StateCompact:12 -h"
        sinfo = gp._output(gp.run_command(gp._slurm(gpu_sinfo, host)))
        live.sinfo_gpu = sinfo[1] if sinfo[0] == 0 else ""
        everyone = gp._output(gp.run_command(gp._slurm('squeue -h -t RUNNING -o "%u|%N"', host)))
        live.others = everyone[1] if everyone[0] == 0 else None
    else:
        sinfo = gp._output(gp.run_command(gp._slurm("sinfo -N -O NodeList,CPUsState -h", host)))
        live.sinfo_cpu = sinfo[1] if sinfo[0] == 0 else ""
    live.cores, live.load1 = local_cores_and_load()
    return live


# --------------------------------------------------------------------------- resolve
def _unreadable_prefixes() -> list[str]:
    raw = os.environ.get("CDX_NODE_UNREADABLE", ",".join(cpu_job.DEFAULT_UNREADABLE))
    return [x.strip() for x in raw.split(",") if x.strip()]


def _declared(spec: Spec, path: str) -> Optional[list[str]]:
    best = None
    for key, info in spec.data.items():
        k = key.rstrip("/")
        if (path == k or path.startswith(k + "/")) and (best is None or len(k) > len(best[0])):
            best = (k, info)
    if best is None or not isinstance(best[1], dict) or best[1].get("readable_from") is None:
        return None
    return [str(x) for x in best[1]["readable_from"]]


def default_mem(req: Request, notes: list[str]) -> str:
    if req.mem:
        return req.mem
    if req.footprint:
        path = os.environ.get("CDX_FOOTPRINTS", cpu_job.FOOTPRINTS)
        try:
            return cpu_job.profile_mem(req.footprint, max(1, req.cpus // max(1, req.threads_per_proc)), path)
        except (OSError, KeyError, ValueError) as exc:
            notes.append(f"footprint {req.footprint!r} not usable ({exc!r}); memory defaults to 16G")
    return "16G"


def resolve(req: Request, spec: Spec, site: dict[str, Any], live: Live) -> "Placement | Rejections":
    rejected: list[tuple[str, str]] = []
    notes: list[str] = []
    classes = [c for c in (req.host_class or CLASSES) if c in CLASSES]
    host_base = live.hostname.split(".")[0]
    # 1. data readability
    allowed = list(classes)
    paths = [os.path.abspath(os.path.join(req.cwd, d)) for d in req.data]
    bad = cpu_job.find_unreadable(list(req.cmd) + paths, req.cwd, _unreadable_prefixes())
    if bad:
        why = "; ".join(f"{label} -> {real}" for label, real in bad[:3])
        for c in ("slurm-step", "slurm-job"):
            if c in allowed:
                allowed.remove(c)
                rejected.append((c, f"data not readable from Slurm nodes ({why})"))
    for p in paths:
        rf = _declared(spec, p)
        if rf is None:
            continue
        keep = {"slurm-step": "slurm" in rf, "slurm-job": "slurm" in rf, "westeros": "westeros" in rf,
                "local": "local" in rf or host_base in rf}
        for c in list(allowed):
            if not keep[c]:
                allowed.remove(c)
                rejected.append((c, f"data {p} readable_from {rf} excludes it"))
    mem = default_mem(req, notes)
    mem_mb = gp.parse_mem_mb(mem)
    base = dict(cpus=req.cpus, gpus=req.gpus, mem=mem, time=req.time)
    running = [j for j in live.queue.jobs if j.state.upper().split("+", 1)[0] == "RUNNING"]
    for cls in allowed:
        if cls == "slurm-step":
            cands = [j for j in running if j.gpus >= req.gpus]
            if not running:
                rejected.append((cls, "no running hold"))
            elif live.node_cap != 1:
                rejected.append((cls, f"node cap {live.node_cap}: a new job can start instead"))
            elif not cands:
                rejected.append((cls, f"hold {running[0].job_id} has {running[0].gpus} GPU(s) < {req.gpus}"))
            elif req.gpu_type and req.gpus and req.gpu_type not in gp.parse_gres(cands[0].gres):
                rejected.append((cls, f"hold {cands[0].job_id} gres {cands[0].gres} lacks type {req.gpu_type}"))
            else:
                job = sorted(cands, key=lambda j: (j.gpus != req.gpus and req.gpus > 0, j.job_id))[0]
                nodes = sorted(gp.running_nodes(gp.QueueStats(0, {}, [job])))
                return Placement("slurm-step", node=nodes[0] if nodes else job.node, hold=job.job_id,
                                 gres=gpu_lock.parse_gres(job.gres) if req.gpus else None, rejected=rejected,
                                 notes=notes, **base)
        elif cls == "slurm-job":
            used = gp.running_nodes(live.queue)
            if req.gpus > 0:
                types = req.gpu_type.split(",") if req.gpu_type else sorted(
                    {t for n in gp.parse_sinfo(live.sinfo_gpu) for t in n.gres})
                others = gp.parse_other_user_nodes(live.others, live.user) if live.others else None
                ev = gp.evaluate(live.sinfo_gpu, live.squeue_text, types, n=req.gpus,
                                 prefixes=site["gpu_prefixes"], cap=live.gpu_cap, node_limit=live.node_cap,
                                 scontrol_text=live.scontrol, mem_mb=mem_mb, cpus=req.cpus,
                                 other_user_nodes=others)
                seen: set[tuple[str, str]] = set()
                for node, why in ev.reasons:
                    if why != "prefix filtered" and (node, why) not in seen:
                        seen.add((node, why))
                        rejected.append((f"slurm-job node={node}", why))
                if ev.choice:
                    c = ev.choice
                    return Placement("slurm-job", node=c.node, gpu_type=c.gpu_type, rejected=rejected, notes=notes,
                                     gres=f"gpu:{c.gpu_type}:{req.gpus}", **base)
            else:
                rows = cpu_job.node_rows(live.sinfo_cpu, live.scontrol or "", site["node_prefixes"], req.cpus, mem_mb)
                choice = None
                for r in rows:
                    why = r[4]
                    if not why and len(used) >= live.node_cap and r[0] not in used:
                        why = (f"would pend: AssocGrpNodeLimit (running jobs on {','.join(sorted(used))}, "
                               f"limit {live.node_cap})")
                    if why:
                        rejected.append((f"slurm-job node={r[0]}", why))
                    elif choice is None:
                        choice = r[0]
                if choice:
                    return Placement("slurm-job", node=choice, rejected=rejected, notes=notes, **base)
        elif cls == "westeros":
            if req.gpus < 1:
                rejected.append((cls, "no GPUs requested"))
            elif not host_base.startswith("westeros"):
                rejected.append((cls, f"this host is {host_base}, not westeros"))
            else:
                return Placement("westeros", node=host_base, rejected=rejected, notes=notes, **base)
        elif cls == "local":
            idle = live.cores - live.load1
            if req.gpus > 0:
                rejected.append((cls, "GPUs requested: use westeros or Slurm"))
            elif idle < req.cpus:
                rejected.append((cls, f"idle cores {idle:.1f} (cores {live.cores} - load1 {live.load1:.1f}) "
                                      f"< {req.cpus}"))
            else:
                return Placement("local", node=host_base, rejected=rejected, notes=notes, **base)
    return Rejections(rejected, notes)


def plan_lines(result: "Placement | Rejections") -> list[str]:
    lines = []
    if isinstance(result, Placement):
        head = f"PLAN class={result.cls}"
        if result.hold:
            head += f" hold={result.hold}"
        if result.node:
            head += f" node={result.node}"
        head += f" cpus={result.cpus} gpus={result.gpus} mem={result.mem} time={result.time}"
        lines.append(head)
    else:
        lines.append("PLAN none")
    lines += [f"rejected {place}: {why}" for place, why in result.rejected]
    lines += [f"note: {n}" for n in result.notes]
    return lines


# --------------------------------------------------------------------------- code snapshot / wrap
def git_state(repo: str) -> dict[str, Any]:
    head = _git(["rev-parse", "HEAD"], repo)
    diff = _git(["diff", "HEAD"], repo) or ""
    status = _git(["status", "--porcelain", "-uno"], repo) or ""
    return {"repo": repo, "head": head.strip() if head else None, "dirty": bool(status.strip()),
            "diff_sha256": hashlib.sha256(diff.encode("utf-8", "replace")).hexdigest(), "snapshot": None}


def snapshot_key(state: dict[str, Any]) -> str:
    return hashlib.sha256(f"{state['head']}\0{state['diff_sha256']}".encode()).hexdigest()[:20]


def tracked_files(repo: str) -> bytes:
    try:
        r = subprocess.run(["git", "ls-files", "-z"], cwd=repo, capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return b""
    return r.stdout if r.returncode == 0 else b""


def is_local(cls: str) -> bool:
    return cls in ("local", "westeros")


def wrap(p: Placement, req: Request, run_dir: str, job_id: str, repo: str, state: Optional[dict[str, Any]] = None,
         site: Optional[dict[str, Any]] = None) -> str:
    """The bash script a placement runs: thread pins, env, scratch dirs, snapshot, cd, CMD, then DONE.json."""
    site = site or SITE_DEFAULTS
    scratch_root = site["local_scratch_root" if is_local(p.cls) else "node_scratch_root"]
    snap_root = site["local_snapshot_root" if is_local(p.cls) else "node_snapshot_root"]
    q = shlex.quote
    tpp = str(max(1, req.threads_per_proc))
    L = ["#!/bin/bash", f"# cdx-agent --run job {job_id} (placement {p.cls})", "set +e", f"RUN_DIR={q(run_dir)}",
         f'JOBDIR="{scratch_root}/{job_id}"', 'mkdir -p "$JOBDIR"', "("]
    rel = os.path.relpath(req.cwd, repo) if req.cwd.startswith(repo) else "."
    if req.snapshot and state is not None:
        key = snapshot_key(state)
        L += [f'  SNAP="{snap_root}/{key}"', '  if [ ! -d "$SNAP" ]; then',
              '    mkdir -p "$SNAP.tmp.$$" && tar -C ' + q(repo)
              + ' --null -T "$RUN_DIR/tracked.list" -cf - | tar -xf - -C "$SNAP.tmp.$$" && mv "$SNAP.tmp.$$" "$SNAP" \\',
              '      || { rm -rf "$SNAP.tmp.$$"; [ -d "$SNAP" ] || exit 70; }', "  fi",
              f'  cd "$SNAP"/{q(rel)} || exit 71']
    else:
        L.append(f"  cd {q(req.cwd)} || exit 71")
    for v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMBA_NUM_THREADS"):
        L.append(f"  export {v}={tpp}")
    for k, v in req.env.items():
        L.append(f"  export {k}={q(v)}")
    for k, sub in req.scratch.items():
        L += [f'  export {k}="$JOBDIR"/{q(sub)}', f'  mkdir -p "${k}"']
    L += [f"  {shlex.join(req.cmd)}", ")", "rc=$?",
          'tmp="$RUN_DIR/DONE.json.tmp.$$"',
          'printf \'{"job_id": "%s", "exit_code": %d, "finished": "%s", "host": "%s"}\\n\' '
          f'{q(job_id)} "$rc" "$(date -Is)" "$(hostname)" > "$tmp" && mv "$tmp" "$RUN_DIR/DONE.json"',
          "exit $rc", ""]
    return "\n".join(L)


def build_command(p: Placement, req: Request, site: dict[str, Any], run_dir: str, job_id: str, who: str,
                  detach: bool, host: Optional[str]) -> list[str]:
    """argv that starts the wrapped script (``bash <run_dir>/job.sh``) in the placement."""
    login = host or os.environ.get("CDX_SLURM_HOST", "rlab1")
    script = os.path.join(run_dir, "job.sh")
    inner = ["bash", script]
    t = parse_time_min(req.time)
    if p.cls == "slurm-step":
        return gpu_lock.build_ssh_command(
            p.hold or "", login, p.gres or "", p.mem, t, req.gpus, None, who, site["lock_dir"], site["python"], inner)
    if p.cls == "slurm-job":
        opts = [f"-w {shlex.quote(p.node or '')}", f"--mem={p.mem}", f"-t {t}", f"-J {shlex.quote(job_id)}"]
        if req.gpus:
            opts.append(f"--gres={p.gres}")
        if detach:
            log = os.path.join(run_dir, "run.log")
            remote = f"sbatch --parsable {' '.join(opts)} -o {shlex.quote(log)} --wrap {shlex.quote(shlex.join(inner))}"
        else:
            remote = f"srun {' '.join(opts)} {shlex.join(inner)}"
        return ["ssh", "-o", "BatchMode=yes", "-o", "LogLevel=ERROR", login, remote]
    if p.cls == "westeros":
        return [site["python"], os.path.abspath(gpu_lock.__file__), "node-run", "--n", str(req.gpus), "--gpus",
                site["westeros_gpus"], "--who", who, "--lock-dir", site["lock_dir"], "--"] + inner
    return inner


# --------------------------------------------------------------------------- run / collect
def collect(run_dir: str, timeout_s: float = 60.0, poll_s: float = 1.0, fallback_rc: Optional[int] = None,
            clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> Optional[dict]:
    """Reads DONE.json (through nfs.wait_visible) and rewrites the manifest. None when it never appeared."""
    done = os.path.join(run_dir, "DONE.json")
    if nfs.wait_visible(done, timeout_s, poll_s, clock=clock, sleep=sleep):
        try:
            with open(done, encoding="utf-8") as fh:
                info = json.load(fh)
        except (OSError, ValueError):
            info = None
        if info is not None:
            rc = int(info.get("exit_code", -1))
            manifest.update(run_dir, finished=info.get("finished"), exit_code=rc,
                            state="succeeded" if rc == 0 else "failed")
            return info
    if fallback_rc is not None:
        manifest.update(run_dir, finished=time.strftime("%Y-%m-%dT%H:%M:%S%z"), exit_code=fallback_rc,
                        state="succeeded" if fallback_rc == 0 else "failed")
    return None


def make_job_id(name: str) -> str:
    return f"{time.strftime('%Y%m%dT%H%M%S')}-{name}-{os.getpid()}"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cdx-agent --run", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--profile")
    p.add_argument("--cpus", type=int)
    p.add_argument("--gpus", type=int)
    p.add_argument("--gpu-type")
    p.add_argument("--gpu-mem")
    p.add_argument("--mem")
    p.add_argument("--disk")
    p.add_argument("--time")
    p.add_argument("--host-class", action="append", help="comma list or repeated; also the preference order")
    p.add_argument("--data", action="append")
    p.add_argument("--snapshot", action="store_true", help="run from a node-local copy of the tracked files")
    p.add_argument("--detach", action="store_true")
    p.add_argument("--name")
    p.add_argument("--plan", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--collect", metavar="RUN_DIR", help="read DONE.json of RUN_DIR and finish its manifest")
    p.add_argument("--timeout", type=float, default=60.0, help="--collect wait for DONE.json (s)")
    p.add_argument("--slurm-host", default=None)
    p.add_argument("--user", default=os.environ.get("USER", ""))
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, cmd = argv[:i], argv[i + 1:]
    args = build_parser().parse_args(argv)
    if args.host_class:
        args.host_class = [c.strip() for part in args.host_class for c in part.split(",") if c.strip()]
    if args.collect:
        info = collect(args.collect, args.timeout)
        print(json.dumps(info) if info else f"run: no DONE.json in {args.collect} after {args.timeout:.0f} s")
        return 0 if info else 1
    if not cmd:
        print("run: no command (use: -- CMD ...)", file=sys.stderr)
        return EXIT_REFUSED
    cwd = os.path.abspath(os.getcwd())
    repo = repo_root(cwd)
    try:
        spec = load_spec(repo)
        site = load_site(repo)
        req = build_request(args, spec, cmd, cwd)
        parse_time_min(req.time)
    except (KeyError, ValueError, OSError) as exc:
        print(f"run: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    live = fetch_live(req, args.user, args.slurm_host)
    result = resolve(req, spec, site, live)
    if args.plan:
        print("\n".join(plan_lines(result)))
        return 0
    if isinstance(result, Rejections):
        print("\n".join(plan_lines(result)), file=sys.stderr)
        print("run: no placement can take this request", file=sys.stderr)
        return EXIT_REFUSED
    job_id = make_job_id(req.name)
    run_dir = os.path.join(repo, ".cdx", "runs", job_id)
    state = git_state(repo)
    if req.snapshot:
        root = site["local_snapshot_root" if is_local(result.cls) else "node_snapshot_root"]
        state["snapshot"] = f"{root}/{snapshot_key(state)}"
    script = wrap(result, req, run_dir, job_id, repo, state, site)
    argv_run = build_command(result, req, site, run_dir, job_id, os.path.basename(repo) or "cdx-agent", args.detach,
                             args.slurm_host)
    shown = " ".join(shlex.quote(x) for x in argv_run)
    if args.dry_run:
        print("\n".join(plan_lines(result)))
        print("# wrapped script (written to <run_dir>/job.sh)")
        print(script)
        print("# command")
        print(shown)
        return 0
    os.makedirs(run_dir, exist_ok=True)
    job_sh = os.path.join(run_dir, "job.sh")
    with open(job_sh, "w", encoding="utf-8") as fh:
        fh.write(script)
    os.chmod(job_sh, 0o755)
    if req.snapshot:
        with open(os.path.join(run_dir, "tracked.list"), "wb") as fh:
            fh.write(tracked_files(repo))
    log = os.path.join(run_dir, "run.log") if args.detach else None
    manifest.write(
        run_dir, job_id=job_id, name=req.name, project=os.path.basename(repo), command=list(cmd), cwd=cwd, code=state,
        env=req.env, host=live.hostname, footprint=req.footprint,
        placement={"class": result.cls, "slurm_job": result.hold, "step": None, "node": result.node,
                   "gpus": result.gpus, "locks": result.cls in ("slurm-step", "westeros") and result.gpus > 0},
        resources={"cpus": result.cpus, "gpus": result.gpus, "gpu_type": req.gpu_type, "gpu_mem": req.gpu_mem,
                   "mem": result.mem, "disk": req.disk, "time": result.time, "threads_per_proc": req.threads_per_proc},
        scratch_dirs={k: f"{v}" for k, v in req.scratch.items()},
        started=time.strftime("%Y-%m-%dT%H:%M:%S%z"), state="running", log=log)
    print(f"run: {plan_lines(result)[0]}", file=sys.stderr)
    print(f"run: {shown}", file=sys.stderr, flush=True)
    done = os.path.join(run_dir, "DONE.json")
    if args.detach:
        if result.cls == "slurm-job":
            r = subprocess.run(argv_run, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            if r.returncode != 0:
                manifest.update(run_dir, state="failed", exit_code=r.returncode)
                print(r.stderr.strip()[-500:], file=sys.stderr)
                return 1
            sj = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else None
            m = manifest.read(run_dir)
            m["placement"]["slurm_job"] = sj
            manifest.update(run_dir, placement=m["placement"])
        else:
            with open(log or os.devnull, "ab") as lf:
                subprocess.Popen(argv_run, stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.STDOUT,
                                 start_new_session=True)
        print(f"run_dir={run_dir} log={log} done={done}")
        return 0
    rc = subprocess.run(argv_run).returncode
    collect(run_dir, timeout_s=30.0, fallback_rc=rc)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
