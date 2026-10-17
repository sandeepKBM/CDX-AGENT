"""Choose an eligible Slurm GPU node before submitting a job.

    python -m cdx_agent.gpu_pick --gpu-type a4000[,4500_ada,...] [--n 1]
        [--prefix rlab,ilab] [--user USER] [--cap 12] [--per-node-clients 4]
        [--shared-clients 2] [--client-jobs REGEX] [--slots-per-gpu 2]
        [--mem 60G] [--min-disk-gb 50] [--explain]

The selector reads ``sinfo``, ``scontrol show node``, the user's active ``squeue``
jobs and the running jobs of all users over ``ssh -o BatchMode=yes -o
ConnectTimeout=10`` to ``$CDX_SLURM_HOST`` or ``rlab1``.  A node is picked only if a
job can really start there: enough free GPUs and free memory (``RealMemory - AllocMem
>= --mem``), a state that is not PLANNED/DRAIN/DOWN/RESERVED/MAINT, room under the
per-node client limit (our running ``--client-jobs`` GPUs x ``--slots-per-gpu`` plus
the new job; ``--per-node-clients`` when we are alone on the node, ``--shared-clients``
when other users run jobs there) and ``--min-disk-gb`` free in /freespace/local (``ssh
<node> df -P``; unreachable = warning, not a rejection).  Output: the chosen node with
the full ``sbatch`` options (including ``--mem``) first, then one ``rejected node=..
reason=..`` line per rejected node.
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple


@dataclass
class GpuNode:
    name: str
    gres: Dict[str, int]
    gres_used: Dict[str, int]
    state: str

    @property
    def free(self) -> Dict[str, int]:
        return {gpu_type: max(0, count - self.gres_used.get(gpu_type, 0))
                for gpu_type, count in self.gres.items()}


@dataclass
class NodeInfo:
    """``scontrol show node -o`` facts: memory in MB and the full state string (e.g. ``MIXED+PLANNED``)."""
    name: str
    real_mb: Optional[int]
    alloc_mb: Optional[int]
    state: str

    @property
    def free_mb(self) -> Optional[int]:
        if self.real_mb is None or self.alloc_mb is None:
            return None
        return self.real_mb - self.alloc_mb


@dataclass
class QueueJob:
    job_id: str
    name: str
    node: str
    gres: str
    state: str

    @property
    def gpus(self) -> int:
        return gpu_count(self.gres)


@dataclass
class QueueStats:
    gpu_count: int
    clients: Dict[str, int]
    jobs: List[QueueJob]


@dataclass
class GpuChoice:
    node: str
    gpu_type: str
    free: int
    clients: int


@dataclass
class Evaluation:
    choice: Optional[GpuChoice]
    reasons: List[Tuple[str, str]]
    rows: List[Tuple[str, str, str, int, int]]
    warnings: List[str] = field(default_factory=list)


def _slurm(command: str, host: Optional[str] = None) -> List[str]:
    host = host or os.environ.get("CDX_SLURM_HOST", "rlab1")
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, command]


def run_command(command: Sequence[str]):
    try:
        return subprocess.run(list(command), capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(list(command), 1, "", str(exc))


def _output(value: Any) -> Tuple[int, str, str]:
    if isinstance(value, str):
        return 0, value, ""
    if isinstance(value, tuple):
        return int(value[0]), str(value[1]), str(value[2]) if len(value) > 2 else ""
    return int(getattr(value, "returncode", 1)), str(getattr(value, "stdout", "") or ""), str(getattr(value, "stderr", "") or "")


def parse_gres(text: str) -> Dict[str, int]:
    """Parse GPU counts from ``gpu:type:N`` GRES tokens."""
    result: Dict[str, int] = {}
    for match in re.finditer(r"(?:^|[\s,])gpu:([^:,\s()]+):(\d+)", text or ""):
        gpu_type, count = match.group(1), int(match.group(2))
        result[gpu_type] = result.get(gpu_type, 0) + count
    # Some squeue versions expose a type-less value as gpu:N.
    if not result:
        match = re.search(r"(?:^|[\s,])gpu:(\d+)(?:\s|$)", text or "")
        if match:
            result["gpu"] = int(match.group(1))
    return result


def gpu_count(text: str) -> int:
    """Return the total number of GPUs requested by a GRES string."""
    counts = parse_gres(text)
    return sum(counts.values())


def _sinfo_fields(line: str) -> Optional[Tuple[str, str, str, str]]:
    if not line.strip():
        return None
    # ``sinfo -O`` uses these minimum widths.  Prefer fixed columns when the
    # line is padded, while retaining a simple four-field form for tests and
    # older Slurm formatting.
    node_field = line[:30].strip()
    if len(line) >= 170 and node_field and " " not in node_field:
        node = node_field
        gres = line[30:90].strip()
        used = line[90:170].strip()
        state = line[170:].strip().split()[0] if line[170:].strip() else ""
        if node and state:
            return node, gres, used, state
    parts = line.split()
    if len(parts) < 4:
        return None
    return parts[0], parts[1], parts[2], parts[-1]


def parse_sinfo(text: str) -> List[GpuNode]:
    """Parse ``NodeList Gres GresUsed StateCompact`` output into nodes."""
    nodes: List[GpuNode] = []
    for line in text.splitlines():
        fields = _sinfo_fields(line)
        if fields is None:
            continue
        name, gres, used, state = fields
        if name.lower() in {"nodelist", "node"}:
            continue
        nodes.append(GpuNode(name, parse_gres(gres), parse_gres(used), state))
    return nodes


def parse_mem_mb(text: str) -> int:
    """``60G`` / ``61440M`` / ``2T`` / ``1024`` (MB, the sbatch default) -> MB."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMGT]?)B?\s*", text or "", re.I)
    if not m:
        raise ValueError(f"bad memory value {text!r} (use e.g. 60G)")
    factor = {"K": 1 / 1024, "": 1, "M": 1, "G": 1024, "T": 1024 * 1024}[m.group(2).upper()]
    return int(float(m.group(1)) * factor)


def parse_scontrol(text: str) -> Dict[str, NodeInfo]:
    """Parse ``scontrol show node -o`` (one ``Key=Value`` line per node)."""
    nodes: Dict[str, NodeInfo] = {}
    for line in text.splitlines():
        fields: Dict[str, str] = {}
        for key, value in re.findall(r"(\w+)=(\S*)", line):
            fields.setdefault(key, value)
        name = fields.get("NodeName")
        if not name:
            continue

        def num(key: str) -> Optional[int]:
            return int(fields[key]) if fields.get(key, "").isdigit() else None

        nodes[name] = NodeInfo(name, num("RealMemory"), num("AllocMem"), fields.get("State", ""))
    return nodes


def parse_other_user_nodes(text: str, user: str) -> Set[str]:
    """Rows ``%u|%N`` of running jobs (all users) -> nodes where somebody other than ``user`` runs a job."""
    nodes: Set[str] = set()
    for line in text.splitlines():
        owner, _, node_list = line.strip().partition("|")
        if not owner or owner == user or owner.lower() in {"user", "%u"}:
            continue
        nodes.update(n.strip() for n in node_list.split(",") if n.strip() and n.strip() not in {"N/A", "(null)", "(none)", "-"})
    return nodes


def parse_df_free_gb(text: str) -> Optional[float]:
    """``df -P`` output -> free GB of the last line (Available column, 1K blocks), or None when unparseable."""
    for line in reversed([l for l in text.splitlines() if l.strip()]):
        parts = line.split()
        if len(parts) >= 6 and parts[3].isdigit():
            return int(parts[3]) / (1024 * 1024)
        break
    return None


def probe_disk(node: str, path: str = "/freespace/local") -> Optional[float]:
    """Free GB at ``path`` on ``node`` over ssh with a short timeout; None = unknown."""
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", node, f"timeout 10 df -P {shlex.quote(path)}"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_df_free_gb(r.stdout) if r.returncode == 0 else None


def parse_squeue(text: str, client_jobs: str = "tower_|isaac", slots_per_gpu: int = 2) -> QueueStats:
    """Parse ``%i|%j|%N|%b|%T`` rows and sum active GPU/client load."""
    jobs: List[QueueJob] = []
    total = 0
    clients: Dict[str, int] = {}
    matcher = re.compile(client_jobs) if client_jobs else None
    for line in text.splitlines():
        fields = line.rstrip("\n").split("|", 4)
        if len(fields) != 5 or fields[0].lower() in {"jobid", "%i"}:
            continue
        job = QueueJob(*fields)
        state = job.state.upper().split("+", 1)[0]
        if state not in {"RUNNING", "PENDING"}:
            continue
        total += job.gpus
        if matcher is not None and matcher.search(job.name):
            for node in job.node.split(","):
                node = node.strip()
                if node and node not in {"N/A", "(null)", "(none)", "-"}:
                    clients[node] = clients.get(node, 0) + job.gpus * slots_per_gpu
        jobs.append(job)
    return QueueStats(total, clients, jobs)


BLOCKED_STATES = ("drain", "drng", "down", "maint", "planned", "plnd", "reserved", "resv", "fail", "inval")


def _state_blocked(state: str) -> bool:
    lowered = state.lower()
    return any(word in lowered for word in BLOCKED_STATES)


def evaluate(sinfo_text: str, squeue_text: str, gpu_types: Sequence[str], *, n: int = 1,
             prefixes: Sequence[str] = ("rlab", "ilab"), cap: int = 12,
             per_node_clients: int = 4, client_jobs: str = "tower_|isaac",
             slots_per_gpu: int = 2, scontrol_text: Optional[str] = None, mem_mb: Optional[int] = None,
             other_user_nodes: Optional[Set[str]] = None, shared_clients: int = 2,
             min_disk_gb: float = 0, disk_probe: Optional[Callable[[str], Optional[float]]] = None) -> Evaluation:
    """Evaluate all nodes and return the best eligible choice plus reasons.

    ``scontrol_text``/``mem_mb`` enable the free-memory and full-state checks, ``other_user_nodes`` the shared-node client
    limit, ``disk_probe``/``min_disk_gb`` the node-local disk check (run last, only on nodes that passed everything else).
    """
    wanted = [gpu_type.strip() for gpu_type in gpu_types if gpu_type.strip()]
    nodes = parse_sinfo(sinfo_text)
    queue = parse_squeue(squeue_text, client_jobs=client_jobs, slots_per_gpu=slots_per_gpu)
    info = parse_scontrol(scontrol_text) if scontrol_text is not None else {}
    others = other_user_nodes or set()
    reasons: List[Tuple[str, str]] = []
    warnings: List[str] = []
    rows: List[Tuple[str, str, str, int, int]] = []
    candidates: List[GpuChoice] = []
    disk_cache: Dict[str, Optional[float]] = {}
    seen: Set[str] = set()
    cap_reason = f"user GPU cap {queue.gpu_count}+{n}>{cap}" if queue.gpu_count + n > cap else ""
    for node in nodes:
        if node.name in seen:               # sinfo -N repeats a node once per partition
            continue
        seen.add(node.name)
        node_clients = queue.clients.get(node.name, 0)
        matching = [(gpu_type, node.free.get(gpu_type, 0)) for gpu_type in wanted]
        best_free = max((free for _, free in matching), default=0)
        rows.append((node.name, node.state, ",".join(f"{gpu_type}={free}" for gpu_type, free in matching) or "-",
                     best_free, node_clients))
        if prefixes and not any(node.name.startswith(prefix) for prefix in prefixes):
            reasons.append((node.name, "prefix filtered"))
            continue
        full_state = info[node.name].state if node.name in info else ""
        if _state_blocked(node.state) or _state_blocked(full_state):
            shown = full_state if _state_blocked(full_state) else node.state
            reasons.append((node.name, f"state={shown} skipped"))
            continue
        if cap_reason:
            reasons.append((node.name, cap_reason))
            continue
        if not matching:
            reasons.append((node.name, "no requested GPU type on node"))
            continue
        if best_free < n:
            details = ", ".join(f"{gpu_type} free={free}" for gpu_type, free in matching)
            reasons.append((node.name, f"need {n}; {details}"))
            continue
        if mem_mb is not None and info:
            node_info = info.get(node.name)
            free_mb = node_info.free_mb if node_info else None
            if free_mb is None:
                warnings.append(f"node={node.name} memory unknown (scontrol), not checked")
            elif free_mb < mem_mb:
                reasons.append((node.name, f"free memory {free_mb // 1024}G < requested {mem_mb // 1024}G "
                                           f"(RealMemory {node_info.real_mb} - AllocMem {node_info.alloc_mb} MB)"))
                continue
        needed_clients = n * slots_per_gpu
        limit = shared_clients if node.name in others else per_node_clients
        if node_clients + needed_clients > limit:
            why = " (other users' jobs share the node)" if node.name in others else ""
            reasons.append((node.name, f"client limit {node_clients}+{needed_clients}>{limit}{why}"))
            continue
        if disk_probe is not None and min_disk_gb > 0:
            if node.name not in disk_cache:
                disk_cache[node.name] = disk_probe(node.name)
            free_gb = disk_cache[node.name]
            if free_gb is None:
                warnings.append(f"node={node.name} /freespace/local free space unknown (ssh df failed), not checked")
            elif free_gb < min_disk_gb:
                reasons.append((node.name, f"/freespace/local free {free_gb:.0f}G < {min_disk_gb:g}G"))
                continue
        for gpu_type, free in matching:
            if free >= n:
                candidates.append(GpuChoice(node.name, gpu_type, free, node_clients))
    choice = min(candidates, key=lambda item: (-item.free, item.clients, item.node, item.gpu_type)) if candidates else None
    return Evaluation(choice, reasons, rows, warnings)


def choose_node(sinfo_text: str, squeue_text: str, gpu_types: Sequence[str], **kwargs: Any) -> Optional[GpuChoice]:
    """Return the best eligible node/type, or ``None``."""
    return evaluate(sinfo_text, squeue_text, gpu_types, **kwargs).choice


def _parser_prefixes(value: str) -> List[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m cdx_agent.gpu_pick", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--gpu-type", required=True, metavar="TYPE[,TYPE...]")
    p.add_argument("--n", type=int, default=1)
    p.add_argument("--prefix", default="rlab,ilab")
    p.add_argument("--user", default=os.environ.get("USER", ""))
    p.add_argument("--cap", type=int, default=12)
    p.add_argument("--per-node-clients", type=int, default=4)
    p.add_argument("--shared-clients", type=int, default=2,
                   help="per-node client limit when other users run jobs on the node (--per-node-clients applies when alone)")
    p.add_argument("--client-jobs", default="tower_|isaac", help="regex on the names of our jobs that run Isaac clients")
    p.add_argument("--slots-per-gpu", type=int, default=2)
    p.add_argument("--mem", default="60G", help="memory the job will request; nodes with less free are rejected")
    p.add_argument("--min-disk-gb", type=float, default=50, help="min free GB in /freespace/local (0 = skip the ssh df check)")
    p.add_argument("--slurm-host", default=None)
    p.add_argument("--explain", action="store_true")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.n < 1 or args.cap < 0 or args.per_node_clients < 0 or args.shared_clients < 0 or args.slots_per_gpu < 0:
        print("gpu_pick: counts must be non-negative and --n must be positive", file=sys.stderr)
        return 2
    try:
        mem_mb = parse_mem_mb(args.mem)
        sinfo = _output(run_command(_slurm("sinfo -N -O NodeList:30,Gres:60,GresUsed:80,StateCompact:12 -h", args.slurm_host)))
        squeue_command = f'squeue -u {shlex.quote(args.user)} -h -o "%i|%j|%N|%b|%T"'
        squeue = _output(run_command(_slurm(squeue_command, args.slurm_host)))
        if sinfo[0] != 0 or squeue[0] != 0:
            detail = (sinfo[2] or squeue[2] or "Slurm query failed").strip().replace("\n", " ")
            print(f"gpu_pick: {detail}", file=sys.stderr)
            return 2
        scontrol = _output(run_command(_slurm("scontrol show node -o", args.slurm_host)))
        everyone = _output(run_command(_slurm('squeue -h -t RUNNING -o "%u|%N"', args.slurm_host)))
        if scontrol[0] != 0:
            print("gpu_pick: warning: scontrol show node failed; memory and full node state not checked", file=sys.stderr)
        if everyone[0] != 0:
            print("gpu_pick: warning: squeue for all users failed; shared-node client limit not applied", file=sys.stderr)
        evaluation = evaluate(sinfo[1], squeue[1], args.gpu_type.split(","), n=args.n,
                              prefixes=_parser_prefixes(args.prefix), cap=args.cap,
                              per_node_clients=args.per_node_clients,
                              client_jobs=args.client_jobs, slots_per_gpu=args.slots_per_gpu,
                              scontrol_text=scontrol[1] if scontrol[0] == 0 else None, mem_mb=mem_mb,
                              other_user_nodes=parse_other_user_nodes(everyone[1], args.user) if everyone[0] == 0 else set(),
                              shared_clients=args.shared_clients, min_disk_gb=args.min_disk_gb,
                              disk_probe=probe_disk if args.min_disk_gb > 0 else None)
    except (re.error, OSError, ValueError) as exc:
        print(f"gpu_pick: {exc}", file=sys.stderr)
        return 2

    if args.explain:
        print("node\tstate\tfree(requested)\tclients")
        for node, state, free, _, clients in evaluation.rows:
            print(f"{node}\t{state}\t{free}\t{clients}")
    for warning in evaluation.warnings:
        print(f"gpu_pick: warning: {warning}", file=sys.stderr)
    choice = evaluation.choice
    if choice is not None:
        print(f'node={choice.node} type={choice.gpu_type} free={choice.free} clients={choice.clients} '
              f'sbatch_opts="--nodelist={choice.node} --gres=gpu:{choice.gpu_type}:{args.n} --mem={args.mem}"')
    seen_reasons: Set[Tuple[str, str]] = set()
    for node, reason in evaluation.reasons:
        if reason == "prefix filtered" and not args.explain:
            continue
        if (node, reason) not in seen_reasons:
            seen_reasons.add((node, reason))
            print(f"rejected node={node} reason={reason}")
    return 0 if choice is not None else 2


if __name__ == "__main__":
    raise SystemExit(main())
