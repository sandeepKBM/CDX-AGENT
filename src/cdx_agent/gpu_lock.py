"""Run a command inside another session's shared Slurm GPU hold, under the node-local GPU lock protocol.

    cdx-agent --gpu-lock HOLD_JOBID [--n K | --gpu G[,G2]] [--eta MIN] [--host rlab1] [--gres gpu:a100:4] [--mem 16G]
                         [--time MIN] [--who NAME] [--no-snapshot] [--dry-run] -- CMD...
    cdx-agent --gpu-lock --status [--node NODE]          (NODE default $CDX_GPU_LOCK_NODE or rlab2; 'local' = here)

Submit side: ssh <host> srun --jobid=HOLD --overlap --gres=<the hold's gres> --mem=M [-t T] \\
                 python3 <this file> node-run ... -- CMD     (--dry-run prints this instead of running it)
Node side (`node-run`), the protocol of the slurm-gpu-allocation skill (sec. 3) and telecom_boed's
slurm/local_tau2_vllm.sh: Slurm does not separate GPUs between --overlap steps, so the step takes K free
locks with flock -n on <lock-dir>/gpu<G>.lock (lowest index first; a GPU whose lock is free but which
already holds > --busy-mib MiB is skipped), exports CUDA_VISIBLE_DEVICES, writes gpu<G>.owner
("<who> host= pid= step= gpu= since= eta= cmd="), runs CMD with the lock fds inherited, and on exit or
SIGTERM/SIGINT/SIGHUP removes its owner files and releases the locks.

NFS snapshot (bash reads scripts lazily; a script on NFS that is edited or mv-replaced while it runs dies
on another NFS client with "Stale file handle", telecom step 526576.16): node-run first copies this
wrapper to <lock-dir>/run/<job>.<step>_<pid>/ on node-local disk and re-execs the copy, and if CMD is a
shell script (`bash x.sh ...`, `sh x.sh`, or an executable with a sh/bash/zsh/ksh/dash shebang) it runs
a node-local copy of that script. The copy's $0 / BASH_SOURCE point into the run dir; the original path is
exported as CDX_GPU_LOCK_SCRIPT_ORIG (its directory as CDX_GPU_LOCK_SCRIPT_DIR). Python scripts are not
copied (Python reads the whole file before running it, and sys.path[0] must stay the script's dir).
--no-snapshot turns both copies off. The run dir is removed at exit if CMD left nothing else in it.

Exit codes: 3 no free GPU lock, 4 free locks only on busy GPUs, 2 usage error, else CMD's exit code.
This file is stdlib-only and runs as a plain script (no package import) so it can execute on Slurm nodes.
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

DEFAULT_LOCK_DIR = "/freespace/local/ss5772/gpu_locks"
DEFAULT_HOST = "rlab1"
DEFAULT_STATUS_NODE = os.environ.get("CDX_GPU_LOCK_NODE", "rlab2")
SHELLS = ("bash", "sh", "dash", "zsh", "ksh")
EXIT_USAGE, EXIT_NO_LOCK, EXIT_BUSY = 2, 3, 4
_SHELL_OPTS_WITH_ARG = ("-o", "+o", "-O", "+O", "--rcfile", "--init-file")


class LockError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- lock primitives
def lock_path(lock_dir: str, gpu: int) -> str:
    return os.path.join(lock_dir, f"gpu{gpu}.lock")


def owner_path(lock_dir: str, gpu: int) -> str:
    return os.path.join(lock_dir, f"gpu{gpu}.owner")


def try_lock(path: str) -> Optional[int]:
    """flock -n equivalent: an fd holding an exclusive lock, or None if another holder has it."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o640)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            return None
        raise
    return fd


def release(taken: Sequence[Tuple[int, int]]) -> None:
    for _, fd in taken:
        try:
            os.close(fd)  # closing the last fd of the open file releases the flock
        except OSError:
            pass


def acquire(lock_dir: str, candidates: Sequence[int], n: int,
            busy_mib: Optional[Dict[int, int]] = None, threshold: int = 1500) -> List[Tuple[int, int]]:
    """Takes n locks (all or nothing), lowest candidate first. Returns [(gpu, fd)] or raises LockError."""
    os.makedirs(lock_dir, exist_ok=True)
    taken: List[Tuple[int, int]] = []
    busy: List[str] = []
    for g in candidates:
        if len(taken) == n:
            break
        fd = try_lock(lock_path(lock_dir, g))
        if fd is None:
            continue
        used = (busy_mib or {}).get(g)
        if used is not None and used > threshold:
            os.close(fd)
            busy.append(f"gpu{g}={used}MiB")
            continue
        taken.append((g, fd))
    if len(taken) < n:
        release(taken)
        if busy:
            raise LockError(EXIT_BUSY, f"need {n} GPU lock(s); free locks only on busy GPUs ({', '.join(busy)} > "
                                       f"{threshold} MiB: someone is not honouring the locks); --busy-mib/--force-busy")
        raise LockError(EXIT_NO_LOCK, f"need {n} free GPU lock(s) under {lock_dir} among {list(candidates)}; "
                                      f"got {len(taken)} (see --status)")
    return taken


def owner_line(who: str, gpu: int, pid: int, host: str, step: str, since: str, eta: str, cmd: Sequence[str]) -> str:
    text = " ".join(shlex.quote(c) for c in cmd).replace("\n", " ")
    if len(text) > 300:
        text = text[:297] + "..."
    return f"{who} host={host} pid={pid} step={step} gpu={gpu} since={since} eta={eta} cmd={text}\n"


def write_owner(lock_dir: str, gpu: int, line: str) -> None:
    final = owner_path(lock_dir, gpu)
    tmp = f"{final}.tmp.{os.getpid()}"
    with open(tmp, "w") as fh:
        fh.write(line)
    os.chmod(tmp, 0o640)
    os.replace(tmp, final)


def remove_owner(lock_dir: str, gpu: int, pid: int) -> bool:
    """Removes gpu<G>.owner only if it is still ours (pid match), like the reference trap."""
    path = owner_path(lock_dir, gpu)
    try:
        with open(path) as fh:
            content = fh.read()
    except OSError:
        return False
    if f" pid={pid} " not in content:
        return False
    try:
        os.remove(path)
    except OSError:
        return False
    return True


def query_busy_mib() -> Optional[Dict[int, int]]:
    """{gpu index: MiB used} from nvidia-smi, or None when it is unavailable."""
    if not shutil.which("nvidia-smi"):
        return None
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=index,memory.used", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    out: Dict[int, int] = {}
    for line in r.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            out[int(parts[0])] = int(parts[1])
    return out


# --------------------------------------------------------------------------- NFS snapshot
def is_shell_script(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            first = fh.readline(256).decode("utf-8", "replace").strip()
    except OSError:
        return False
    if not first.startswith("#!"):
        return False
    words = first[2:].split()
    if not words:
        return False
    interp = os.path.basename(words[0])
    if interp == "env":
        rest = [w for w in words[1:] if not w.startswith("-")]
        interp = os.path.basename(rest[0]) if rest else ""
    return interp in SHELLS


def script_index(cmd: Sequence[str]) -> Optional[int]:
    """Index of the shell script file in CMD that bash would read lazily, or None."""
    if not cmd:
        return None
    if os.path.basename(cmd[0]) in SHELLS:
        i = 1
        while i < len(cmd):
            a = cmd[i]
            if a == "--":
                i += 1
                break
            if a.startswith("-") and not a.startswith("--") and "c" in a[1:]:
                return None  # `bash -c "..."` / `bash -lc`: a command string, no script file
            if a in _SHELL_OPTS_WITH_ARG:
                i += 2
                continue
            if a.startswith("-") or a.startswith("+"):
                i += 1
                continue
            break
        if i < len(cmd) and os.path.isfile(cmd[i]):
            return i
        return None
    if "/" in cmd[0] and os.path.isfile(cmd[0]) and is_shell_script(cmd[0]):
        return 0
    return None


def snapshot_command(cmd: Sequence[str], run_dir: str) -> Tuple[List[str], List[str], Dict[str, str]]:
    """Copies CMD's shell script (if any) into run_dir. Returns (new_cmd, copied paths, extra env)."""
    new = list(cmd)
    idx = script_index(cmd)
    if idx is None:
        return new, [], {}
    orig = os.path.abspath(cmd[idx])
    os.makedirs(run_dir, exist_ok=True)
    dst = os.path.join(run_dir, os.path.basename(orig))
    k = 1
    while os.path.exists(dst):
        dst = os.path.join(run_dir, f"{k}_{os.path.basename(orig)}")
        k += 1
    shutil.copy2(orig, dst)
    new[idx] = dst
    return new, [dst], {"CDX_GPU_LOCK_SCRIPT_ORIG": orig, "CDX_GPU_LOCK_SCRIPT_DIR": os.path.dirname(orig)}


def run_dir_for(lock_dir: str, pid: Optional[int] = None) -> str:
    job = os.environ.get("SLURM_JOB_ID", "local")
    step = os.environ.get("SLURM_STEP_ID", "0")
    return os.path.join(lock_dir, "run", f"{job}.{step}_{pid if pid is not None else os.getpid()}")


def snapshot_self(run_dir: str) -> str:
    os.makedirs(run_dir, exist_ok=True)
    dst = os.path.join(run_dir, "gpu_lock.py")
    shutil.copy2(os.path.abspath(__file__), dst)
    return dst


def cleanup_run_dir(run_dir: str, created: Sequence[str]) -> Optional[str]:
    """Removes only files this wrapper created, then the dir if empty. Returns a note if something stays."""
    for path in created:
        try:
            os.remove(path)
        except OSError:
            pass
    try:
        os.rmdir(run_dir)
    except FileNotFoundError:
        return None
    except OSError:
        return f"left {run_dir} in place (CMD wrote files into it)"
    return None


# --------------------------------------------------------------------------- node side
def parse_gpus(spec: str) -> List[int]:
    out: List[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


def node_main(argv: Sequence[str], cmd: List[str]) -> int:
    p = argparse.ArgumentParser(prog="gpu_lock.py node-run")
    p.add_argument("--n", type=int, default=1)
    p.add_argument("--gpus", default="0-3", help="candidate GPU indices, e.g. 0-3 or 0,2")
    p.add_argument("--eta", type=float, help="minutes, written into the owner file")
    p.add_argument("--who", default="cdx-agent")
    p.add_argument("--lock-dir", default=DEFAULT_LOCK_DIR)
    p.add_argument("--busy-mib", type=int, default=1500)
    p.add_argument("--force-busy", action="store_true", help="skip the nvidia-smi idle check")
    p.add_argument("--no-snapshot", action="store_true")
    args = p.parse_args(argv)
    if not cmd:
        print("gpu-lock node-run: missing -- CMD", file=sys.stderr)
        return EXIT_USAGE
    pid = os.getpid()
    created: List[str] = []
    run_dir = os.environ.get("CDX_GPU_LOCK_RUN_DIR") or run_dir_for(args.lock_dir, pid)
    me = os.path.abspath(__file__)
    if not args.no_snapshot:
        if os.environ.get("CDX_GPU_LOCK_WRAPPER") != me:
            copy = snapshot_self(run_dir)
            env = dict(os.environ, CDX_GPU_LOCK_WRAPPER=copy, CDX_GPU_LOCK_RUN_DIR=run_dir)
            sys.stdout.flush()
            sys.stderr.flush()
            os.execve(sys.executable, [sys.executable, "-I", copy, "node-run"] + list(argv) + ["--"] + cmd, env)
        created.append(me)  # we are the node-local copy: remove it at exit
    candidates = parse_gpus(args.gpus)
    if args.n < 1 or args.n > len(candidates):
        print(f"gpu-lock node-run: --n {args.n} outside 1..{len(candidates)} candidates", file=sys.stderr)
        cleanup_run_dir(run_dir, created)
        return EXIT_USAGE
    busy = None if args.force_busy else query_busy_mib()
    try:
        taken = acquire(args.lock_dir, candidates, args.n, busy, args.busy_mib)
    except LockError as exc:
        print(f"gpu-lock: {exc}", file=sys.stderr)
        cleanup_run_dir(run_dir, created)
        return exc.code
    gpus = [g for g, _ in taken]
    host = socket.gethostname()
    step = f"{os.environ.get('SLURM_JOB_ID', 'local')}.{os.environ.get('SLURM_STEP_ID', '0')}"
    now = datetime.now().astimezone()
    since = now.isoformat(timespec="seconds")
    eta = (now + timedelta(minutes=args.eta)).isoformat(timespec="seconds") if args.eta else "unknown"
    proc: Optional[subprocess.Popen] = None
    rc = 1
    try:
        for g in gpus:
            write_owner(args.lock_dir, g, owner_line(args.who, g, pid, host, step, since, eta, cmd))
        run_cmd, copies, extra_env = (list(cmd), [], {}) if args.no_snapshot else snapshot_command(cmd, run_dir)
        created.extend(copies)
        env = dict(os.environ, **extra_env)
        env["CUDA_VISIBLE_DEVICES"] = ",".join(str(g) for g in gpus)
        env.setdefault("CUDA_DEVICE_ORDER", "PCI_BUS_ID")  # CUDA index == nvidia-smi index == lock index
        env["CDX_GPU_LOCKS"] = ",".join(lock_path(args.lock_dir, g) for g in gpus)
        note = f"; snapshot {copies[0]}" if copies else ""
        print(f"gpu-lock: {host} step {step} locked gpu {','.join(map(str, gpus))} -> CUDA_VISIBLE_DEVICES="
              f"{env['CUDA_VISIBLE_DEVICES']}{note}", file=sys.stderr, flush=True)

        received: List[int] = []

        def forward(signum, _frame):
            received.append(signum)
            if proc is not None and proc.poll() is None:
                try:
                    os.killpg(proc.pid, signum)
                except OSError:
                    pass

        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, forward)
        if received:  # stopped before CMD started
            return 128 + received[0]
        proc = subprocess.Popen(run_cmd, env=env, pass_fds=[fd for _, fd in taken], start_new_session=True)
        rc = proc.wait()
        if rc < 0:
            rc = 128 - rc
    finally:
        for g in gpus:
            remove_owner(args.lock_dir, g, pid)
        release(taken)
        left = cleanup_run_dir(run_dir, created)
        if left:
            print(f"gpu-lock: {left}", file=sys.stderr)
    return rc


# --------------------------------------------------------------------------- status
def parse_proc_locks(text: str) -> Dict[Tuple[int, int, int], int]:
    """{(major, minor, inode): pid} of granted locks in /proc/locks format."""
    out: Dict[Tuple[int, int, int], int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 6 or parts[1] == "->":
            continue
        try:
            maj, mnr, ino = parts[5].split(":")
            out[(int(maj, 16), int(mnr, 16), int(ino))] = int(parts[4])
        except ValueError:
            continue
    return out


def status_lines(lock_dir: str, proc_locks: Optional[str] = None) -> List[str]:
    if not os.path.isdir(lock_dir):
        return [f"no lock dir {lock_dir} on {socket.gethostname()}"]
    if proc_locks is None:
        try:
            with open("/proc/locks") as fh:
                proc_locks = fh.read()
        except OSError:
            proc_locks = ""
    held = parse_proc_locks(proc_locks)
    gpus = set()
    for name in os.listdir(lock_dir):
        m = re.fullmatch(r"gpu(\d+)\.(lock|owner)", name)
        if m:
            gpus.add(int(m.group(1)))
    lines = [f"{socket.gethostname()} {lock_dir}:"]
    for g in sorted(gpus):
        lp, op = lock_path(lock_dir, g), owner_path(lock_dir, g)
        holder = None
        if os.path.exists(lp):
            st = os.stat(lp)
            holder = held.get((os.major(st.st_dev), os.minor(st.st_dev), st.st_ino))
        try:
            with open(op) as fh:
                owner = fh.read().strip()
        except OSError:
            owner = ""
        if holder is not None:
            lines.append(f"  gpu{g} LOCKED (flock pid {holder}) owner: {owner or '(no owner file)'}")
        elif owner:
            lines.append(f"  gpu{g} free (stale owner file: {owner})")
        else:
            lines.append(f"  gpu{g} free")
    if not gpus:
        lines.append("  no gpu<N>.lock files yet")
    return lines


def status_main(argv: Sequence[str]) -> int:
    p = argparse.ArgumentParser(prog="cdx-agent --gpu-lock --status")
    p.add_argument("--status", action="store_true")
    p.add_argument("--dry-run", action="store_true", help=argparse.SUPPRESS)  # status is read-only anyway
    p.add_argument("--node", default=DEFAULT_STATUS_NODE, help="node holding the locks, or 'local'")
    p.add_argument("--lock-dir", default=DEFAULT_LOCK_DIR)
    args = p.parse_args(argv)
    if args.node in ("local", "localhost", socket.gethostname(), socket.gethostname().split(".")[0]):
        print("\n".join(status_lines(args.lock_dir)))
        return 0
    with open(os.path.abspath(__file__), "rb") as fh:
        source = fh.read()
    remote = f"python3 - status-local --lock-dir {shlex.quote(args.lock_dir)}"
    try:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "LogLevel=ERROR",
                            args.node, remote], input=source, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"gpu-lock --status: ssh {args.node} failed: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(r.stdout.decode("utf-8", "replace"))
    if r.returncode != 0:
        sys.stderr.write(r.stderr.decode("utf-8", "replace")[-1500:])
    return r.returncode


# --------------------------------------------------------------------------- submit side
def parse_gres(text: str) -> Optional[str]:
    """'gres/gpu:a100:4' / 'gres:gpu:a100:4' / 'gpu:4' -> 'gpu:a100:4' / 'gpu:4'."""
    m = re.search(r"gpu(?::([A-Za-z0-9_.\-]+))?[:=](\d+)", text or "")
    if not m:
        return None
    return f"gpu:{m.group(1)}:{m.group(2)}" if m.group(1) else f"gpu:{m.group(2)}"


def query_hold(host: str, hold: str) -> Tuple[Optional[str], Optional[str], str]:
    """(state, gres, detail) of the hold from a read-only `squeue` on the login host."""
    try:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "LogLevel=ERROR", host,
                            f"squeue -h -j {shlex.quote(hold)} -o '%T|%N|%b'"],
                           capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, None, f"squeue via ssh {host} failed: {exc}"
    line = next((ln for ln in r.stdout.splitlines() if "|" in ln), "")
    if not line:
        return None, None, f"hold {hold} not found by squeue on {host} ({r.stderr.strip()[-200:]})"
    state, nodes, gres = (line.split("|") + ["", "", ""])[:3]
    return state.strip(), parse_gres(gres), f"hold {hold} {state.strip()} on {nodes.strip()} gres {gres.strip()}"


def gres_count(gres: str) -> int:
    return int(gres.rsplit(":", 1)[-1])


def default_who() -> str:
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return os.path.basename(r.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        pass
    return os.path.basename(os.getcwd()) or "cdx-agent"


def build_ssh_command(hold: str, host: str, gres: str, mem: str, time_min: Optional[int], n: int,
                      eta: Optional[float], who: str, lock_dir: str, python: str, cmd: Sequence[str],
                      no_snapshot: bool = False, force_busy: bool = False, wrapper: Optional[str] = None,
                      gpus: Optional[str] = None) -> List[str]:
    node = [python, wrapper or os.path.abspath(__file__), "node-run", "--n", str(n),
            "--gpus", gpus or f"0-{gres_count(gres) - 1}", "--who", who, "--lock-dir", lock_dir]
    if eta:
        node += ["--eta", f"{eta:g}"]
    if no_snapshot:
        node.append("--no-snapshot")
    if force_busy:
        node.append("--force-busy")
    srun = ["srun", f"--jobid={hold}", "--overlap", f"--gres={gres}", f"--mem={mem}"]
    if time_min:
        srun += ["-t", str(int(time_min))]
    remote = " ".join(shlex.quote(x) for x in srun + node + ["--"] + list(cmd))
    return ["ssh", "-o", "BatchMode=yes", host, remote]


def submit_main(argv: Sequence[str], cmd: List[str]) -> int:
    p = argparse.ArgumentParser(prog="cdx-agent --gpu-lock", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("hold", help="job id of the shared GPU hold (owned by another session; never scancel it)")
    p.add_argument("--n", type=int, default=1, help="GPUs (locks) to take (default 1)")
    p.add_argument("--gpu", help="lock exactly these GPU indices of the hold (e.g. 2 or 1,3), all or nothing; "
                                 "use when peers agreed on indices (default: the lowest K free)")
    p.add_argument("--eta", type=float, help="expected minutes, written into the owner file")
    p.add_argument("--host", default=DEFAULT_HOST, help="Slurm login host for ssh (default rlab1)")
    p.add_argument("--gres", help="the hold's gres, e.g. gpu:a100:4 (default: read from squeue)")
    p.add_argument("--mem", default="16G", help="step --mem; the hold's memory is shared by all steps (default 16G)")
    p.add_argument("--time", type=int, help="step -t in minutes (default 2 x --eta; none = the hold's limit)")
    p.add_argument("--who", help="owner tag (default: git repo / cwd name)")
    p.add_argument("--lock-dir", default=DEFAULT_LOCK_DIR)
    p.add_argument("--python", default="python3", help="python on the node (default python3)")
    p.add_argument("--no-snapshot", action="store_true", help="do not copy the wrapper / shell script node-locally")
    p.add_argument("--force-busy", action="store_true", help="skip the node's nvidia-smi idle check")
    p.add_argument("--dry-run", action="store_true", help="print the ssh/srun command and exit")
    args = p.parse_args(argv)
    if not cmd:
        print("gpu-lock: missing -- CMD...", file=sys.stderr)
        return EXIT_USAGE
    if not re.fullmatch(r"\d+", args.hold):
        print(f"gpu-lock: HOLD_JOBID must be a job number, got {args.hold!r}", file=sys.stderr)
        return EXIT_USAGE
    gres = parse_gres(args.gres) if args.gres else None
    if args.gres and not gres:
        print(f"gpu-lock: cannot parse --gres {args.gres!r} (want gpu[:type]:count)", file=sys.stderr)
        return EXIT_USAGE
    if gres is None:
        state, gres, detail = query_hold(args.host, args.hold)
        print(f"gpu-lock: {detail}", file=sys.stderr)
        if gres is None:
            print("gpu-lock: pass --gres gpu:<type>:<count> explicitly", file=sys.stderr)
            return EXIT_USAGE
        if state != "RUNNING" and not args.dry_run:
            print(f"gpu-lock: hold {args.hold} is {state}, not RUNNING", file=sys.stderr)
            return EXIT_USAGE
    if args.n < 1 or args.n > gres_count(gres):
        print(f"gpu-lock: --n {args.n} outside 1..{gres_count(gres)} ({gres})", file=sys.stderr)
        return EXIT_USAGE
    n, gpus = args.n, None
    if args.gpu:
        try:
            idx = parse_gpus(args.gpu)
        except ValueError:
            idx = []
        if not idx or len(set(idx)) != len(idx) or any(g < 0 or g >= gres_count(gres) for g in idx):
            print(f"gpu-lock: --gpu {args.gpu!r}: want distinct indices in 0..{gres_count(gres) - 1} ({gres})",
                  file=sys.stderr)
            return EXIT_USAGE
        if args.n not in (1, len(idx)):
            print(f"gpu-lock: --gpu {args.gpu!r} names {len(idx)} GPU(s) but --n is {args.n}", file=sys.stderr)
            return EXIT_USAGE
        n, gpus = len(idx), ",".join(str(g) for g in sorted(idx))
    time_min = args.time or (int(max(10, round(2 * args.eta))) if args.eta else None)
    ssh_cmd = build_ssh_command(args.hold, args.host, gres, args.mem, time_min, n, args.eta,
                                args.who or default_who(), args.lock_dir, args.python, cmd,
                                no_snapshot=args.no_snapshot, force_busy=args.force_busy, gpus=gpus)
    line = " ".join(shlex.quote(x) for x in ssh_cmd)
    if args.dry_run:
        print(line)
        if not time_min:
            print("# note: no -t given (no --eta/--time): the step may run up to the hold's own time limit")
        return 0
    print(f"gpu-lock: {line}", file=sys.stderr, flush=True)
    os.execvp(ssh_cmd[0], ssh_cmd)
    return 1  # pragma: no cover


def split_cmd(argv: Sequence[str]) -> Tuple[List[str], List[str]]:
    argv = list(argv)
    if "--" in argv:
        i = argv.index("--")
        return argv[:i], argv[i + 1:]
    return argv, []


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "node-run":
        opts, cmd = split_cmd(argv[1:])
        return node_main(opts, cmd)
    if argv and argv[0] == "status-local":
        p = argparse.ArgumentParser(prog="gpu_lock.py status-local")
        p.add_argument("--lock-dir", default=DEFAULT_LOCK_DIR)
        print("\n".join(status_lines(p.parse_args(argv[1:]).lock_dir)))
        return 0
    opts, cmd = split_cmd(argv)
    if "--status" in opts:
        return status_main(opts)
    return submit_main(opts, cmd)


if __name__ == "__main__":
    raise SystemExit(main())
