"""`cdx-agent --hash-repro`: run a metadata-only identity command and check the sha256 it prints.

    python -m cdx_agent.hash_repro --want sha256:<hex|prefix> [--timeout S] [--log FILE.jsonl] -- CMD...

Runs CMD CPU-only (CUDA hidden, 2 threads, nice 15, timeout default 1800 s), collects every ``sha256:<hex>`` it prints and
passes only if one starts with --want. Prints one line, ``hash-repro PASS|FAIL ...``. Exit 0 PASS, 1 FAIL, 2 usage error.
Output goes to a temp file under $TMPDIR (kept, its path is in the result line).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from typing import List, Optional, Sequence

USAGE = "usage: cdx-agent --hash-repro --want sha256:<hex|prefix> [--timeout S] [--log FILE.jsonl] -- <command...>"
DEFAULT_TIMEOUT = 1800.0
HASH_RE = re.compile(r"sha256:[0-9a-fA-F]{8,64}")
TIMEOUT_RC = 124
NOT_FOUND_RC = 127


class UsageError(Exception):
    pass


def parse_args(argv: Sequence[str]) -> tuple[str, float, str, List[str]]:
    want = ""
    timeout = DEFAULT_TIMEOUT
    log = ""
    cmd: List[str] = []
    args = list(argv)
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--want", "--timeout", "--log"):
            if i + 1 >= len(args) or args[i + 1] == "--":
                raise UsageError(f"hash-repro: {a} needs a value")
            value = args[i + 1]
            if a == "--want":
                want = value
            elif a == "--log":
                log = value
            else:
                try:
                    timeout = float(value)
                except ValueError:
                    raise UsageError(f"hash-repro: --timeout needs seconds, got {value!r}") from None
                if timeout <= 0:
                    raise UsageError("hash-repro: --timeout must be positive")
            i += 2
        elif a == "--":
            cmd = args[i + 1:]
            break
        else:
            raise UsageError(f"hash-repro: unknown argument {a} (use -- before the command)")
    if not want or not cmd:
        raise UsageError(USAGE)
    return want, timeout, log, cmd


def _child_env() -> dict:
    env = dict(os.environ)
    env.update({"CUDA_VISIBLE_DEVICES": "", "OMP_NUM_THREADS": "2", "MKL_NUM_THREADS": "2",
                "OPENBLAS_NUM_THREADS": "2", "NUMEXPR_NUM_THREADS": "2", "PYTHONDONTWRITEBYTECODE": "1"})
    return env


def _nice() -> None:
    try:
        os.nice(15)
    except OSError:
        pass


def run_command(cmd: Sequence[str], timeout: float, out_path: str) -> int:
    with open(out_path, "wb") as out:
        try:
            proc = subprocess.run(list(cmd), stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                  env=_child_env(), preexec_fn=_nice, timeout=timeout)
        except subprocess.TimeoutExpired:
            return TIMEOUT_RC
        except FileNotFoundError:
            out.write(f"hash-repro: command not found: {cmd[0]}\n".encode())
            return NOT_FOUND_RC
        except PermissionError:
            out.write(f"hash-repro: command not executable: {cmd[0]}\n".encode())
            return 126
    return proc.returncode


def collect_hashes(text: str) -> List[str]:
    got: List[str] = []
    for h in HASH_RE.findall(text):
        h = h.lower()
        if h not in got:
            got.append(h)
    return got


def matches(want: str, got: Sequence[str]) -> bool:
    w = want.lower()
    if not w.startswith("sha256:"):
        w = "sha256:" + w
    return any(h.startswith(w) for h in got)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args in (["--help"], ["-h"]):
        print(USAGE)
        return 0
    try:
        want, timeout, log, cmd = parse_args(args)
    except UsageError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    fd, out_path = tempfile.mkstemp(prefix="hash_repro.", dir=os.environ.get("TMPDIR") or tempfile.gettempdir())
    os.close(fd)
    rc = run_command(cmd, timeout, out_path)
    with open(out_path, errors="replace") as f:
        got = collect_hashes(f.read())
    ok = matches(want, got)
    print("hash-repro %s want=%s got=%s exit=%d log=%s" % ("PASS" if ok else "FAIL", want, ",".join(got) or "-", rc, out_path))
    if log:
        with open(log, "a") as f:
            f.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "want": want, "got": got, "pass": ok,
                                "cmd": cmd, "exit": rc, "out": out_path}) + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
