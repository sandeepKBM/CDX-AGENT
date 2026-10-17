"""Choose an eligible Slurm GPU node before submitting a job.

    python -m cdx_agent.gpu_pick --gpu-type a4000[,4500_ada,...] [--n 1]
        [--prefix rlab,ilab] [--user USER] [--cap 12] [--per-node-clients 4]
        [--client-jobs REGEX] [--slots-per-gpu 2] [--explain]

The selector reads ``sinfo`` and the user's active ``squeue`` jobs over
``ssh -o BatchMode=yes -o ConnectTimeout=10`` to ``$CDX_SLURM_HOST`` or
``rlab1``.  It prints an ``sbatch``-ready nodelist and GRES option when a
node has enough free GPUs and client capacity.
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


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


def parse_squeue(text: str, client_jobs: str = "tower_", slots_per_gpu: int = 2) -> QueueStats:
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


def _state_blocked(state: str) -> bool:
    lowered = state.lower()
    return any(word in lowered for word in ("drain", "down", "maint"))


def evaluate(sinfo_text: str, squeue_text: str, gpu_types: Sequence[str], *, n: int = 1,
             prefixes: Sequence[str] = ("rlab", "ilab"), cap: int = 12,
             per_node_clients: int = 4, client_jobs: str = "tower_",
             slots_per_gpu: int = 2) -> Evaluation:
    """Evaluate all nodes and return the best eligible choice plus reasons."""
    wanted = [gpu_type.strip() for gpu_type in gpu_types if gpu_type.strip()]
    nodes = parse_sinfo(sinfo_text)
    queue = parse_squeue(squeue_text, client_jobs=client_jobs, slots_per_gpu=slots_per_gpu)
    reasons: List[Tuple[str, str]] = []
    rows: List[Tuple[str, str, str, int, int]] = []
    candidates: List[GpuChoice] = []
    cap_reason = f"user GPU cap {queue.gpu_count}+{n}>{cap}" if queue.gpu_count + n > cap else ""
    for node in nodes:
        node_clients = queue.clients.get(node.name, 0)
        matching = [(gpu_type, node.free.get(gpu_type, 0)) for gpu_type in wanted]
        best_free = max((free for _, free in matching), default=0)
        rows.append((node.name, node.state, ",".join(f"{gpu_type}={free}" for gpu_type, free in matching) or "-",
                     best_free, node_clients))
        if prefixes and not any(node.name.startswith(prefix) for prefix in prefixes):
            reasons.append((node.name, "prefix filtered"))
            continue
        if _state_blocked(node.state):
            reasons.append((node.name, f"state={node.state} skipped"))
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
        needed_clients = n * slots_per_gpu
        if node_clients + needed_clients > per_node_clients:
            reasons.append((node.name, f"client limit {node_clients}+{needed_clients}>{per_node_clients}"))
            continue
        for gpu_type, free in matching:
            if free >= n:
                candidates.append(GpuChoice(node.name, gpu_type, free, node_clients))
    choice = min(candidates, key=lambda item: (-item.free, item.clients, item.node, item.gpu_type)) if candidates else None
    return Evaluation(choice, reasons, rows)


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
    p.add_argument("--client-jobs", default="tower_")
    p.add_argument("--slots-per-gpu", type=int, default=2)
    p.add_argument("--slurm-host", default=None)
    p.add_argument("--explain", action="store_true")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.n < 1 or args.cap < 0 or args.per_node_clients < 0 or args.slots_per_gpu < 0:
        print("gpu_pick: counts must be non-negative and --n must be positive", file=sys.stderr)
        return 2
    try:
        sinfo = _output(run_command(_slurm("sinfo -N -O NodeList:30,Gres:60,GresUsed:80,StateCompact:12 -h", args.slurm_host)))
        squeue_command = f'squeue -u {shlex.quote(args.user)} -h -o "%i|%j|%N|%b|%T"'
        squeue = _output(run_command(_slurm(squeue_command, args.slurm_host)))
        if sinfo[0] != 0 or squeue[0] != 0:
            detail = (sinfo[2] or squeue[2] or "Slurm query failed").strip().replace("\n", " ")
            print(f"gpu_pick: {detail}", file=sys.stderr)
            return 2
        evaluation = evaluate(sinfo[1], squeue[1], args.gpu_type.split(","), n=args.n,
                              prefixes=_parser_prefixes(args.prefix), cap=args.cap,
                              per_node_clients=args.per_node_clients,
                              client_jobs=args.client_jobs, slots_per_gpu=args.slots_per_gpu)
    except (re.error, OSError, ValueError) as exc:
        print(f"gpu_pick: {exc}", file=sys.stderr)
        return 2

    if args.explain:
        print("node\tstate\tfree(requested)\tclients")
        for node, state, free, _, clients in evaluation.rows:
            print(f"{node}\t{state}\t{free}\t{clients}")
        for node, reason in evaluation.reasons:
            print(f"reason\t{node}\t{reason}")
    if evaluation.choice is None:
        for node, reason in evaluation.reasons:
            print(f"node={node} reason={reason}")
        return 2
    choice = evaluation.choice
    print(f'node={choice.node} type={choice.gpu_type} free={choice.free} clients={choice.clients} '
          f'sbatch_opts="--nodelist={choice.node} --gres=gpu:{choice.gpu_type}:{args.n}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
