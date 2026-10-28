"""Verified, size-budgeted cache for a shared cold-bank directory.

    python -m cdx_agent.coldbank list
    python -m cdx_agent.coldbank put SRC NAME --meta source=... --meta owner=... \
        --meta regenerable=yes|no --meta cost=... [--zstd] [--update]
    python -m cdx_agent.coldbank fetch NAME [--only REL ...] [--streams N]
    python -m cdx_agent.coldbank run NAME -- CMD...
    python -m cdx_agent.coldbank release NAME [--id ID]
    python -m cdx_agent.coldbank cat NAME REL
    python -m cdx_agent.coldbank status | gc | verify NAME
"""
from __future__ import annotations

import argparse
import concurrent.futures
import errno
import fcntl
import fnmatch
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple


EXIT_OK, EXIT_FAIL, EXIT_REFUSED, EXIT_NO_ROOM = 0, 1, 2, 3
DEFAULT_BANK = "westeros:/data/local/ss5772/coldbank"
DEFAULT_CONFIG = os.path.expanduser("~/.config/cdx-agent/coldbank.json")
REQUIRED_META = ("source", "owner", "regenerable", "cost")
NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{64}$")

BUILTIN_HOSTS = (
    {"pattern": "ilabu", "cache": "/var/tmp/ss5772/coldcache", "budget_gb": 6},
    {"pattern": "ilab*", "cache": "/freespace/local/ss5772/coldcache", "budget_gb": 200},
    {"pattern": "rlab*", "cache": "/freespace/local/ss5772/coldcache", "budget_gb": 200},
    {"pattern": "westeros", "cache": None, "budget_gb": None},
)


class ColdBankError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class BankItem:
    name: str
    root: str
    manifest: Dict[str, str]
    files: Dict[str, str]
    sizes: Dict[str, int]
    meta: Dict[str, str]
    compressed: bool
    archive: Optional[str]


@dataclass(frozen=True)
class FetchPlan:
    name: str
    need: Tuple[str, ...]
    need_bytes: int
    evict: Tuple[str, ...]
    used_bytes: int
    budget_bytes: int
    free_bytes: int
    fits: bool
    reason: str = ""


def _this_host() -> str:
    return socket.gethostname().split(".", 1)[0]


def _short_host(host: str) -> str:
    return host.split(".", 1)[0]


def parse_bank(spec: str) -> Tuple[Optional[str], str, bool]:
    """Return ``(host, path, is_local)`` for a bank specification."""
    if not isinstance(spec, str) or not spec.strip():
        raise ValueError("bank specification is empty")

    value = spec.strip()
    if ":" in value and not value.startswith(("/", "./", "../", "~/")):
        host, path = value.split(":", 1)
        if not host or not path or any(ch.isspace() for ch in host) or "/" in host:
            raise ValueError(f"invalid bank specification: {spec!r}")
        return host, path, _short_host(host) == _this_host()

    return None, os.path.abspath(os.path.expanduser(value)), True


def load_host_table(config_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Load built-in host settings and merge a JSON override."""
    table = [dict(row) for row in BUILTIN_HOSTS]
    path = config_path or os.environ.get("CDX_COLDBANK_CONFIG") or DEFAULT_CONFIG
    path = os.path.expanduser(path)

    if not os.path.isfile(path):
        return table

    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ColdBankError(EXIT_REFUSED, f"cannot read config {path}: {exc}") from exc

    hosts = data.get("hosts", data) if isinstance(data, dict) else None
    if not isinstance(hosts, dict):
        raise ColdBankError(EXIT_REFUSED, f"config {path} has no hosts mapping")

    indexes = {row["pattern"]: i for i, row in enumerate(table)}
    for pattern, value in hosts.items():
        if not isinstance(value, dict):
            raise ColdBankError(EXIT_REFUSED, f"host entry {pattern!r} is not an object")
        row = dict(table[indexes[pattern]]) if pattern in indexes else {"pattern": str(pattern)}
        row.update(value)
        if pattern in indexes:
            table[indexes[pattern]] = row
        else:
            table.append(row)
    return table


def host_settings(host: Optional[str] = None, config_path: Optional[str] = None) -> Dict[str, Any]:
    name = _short_host(host or _this_host())
    for row in load_host_table(config_path):
        if fnmatch.fnmatch(name, str(row.get("pattern", ""))):
            return dict(row)
    return {"pattern": name, "cache": None, "budget_gb": None}


def validate_name(name: str) -> str:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name) or name.startswith("."):
        raise ColdBankError(EXIT_REFUSED, f"bad item name: {name!r}")
    return name


def validate_rel(path: str) -> str:
    if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path:
        raise ColdBankError(EXIT_REFUSED, f"bad relative path: {path!r}")
    if any(part in ("", ".", "..") for part in path.split("/")):
        raise ColdBankError(EXIT_REFUSED, f"bad relative path: {path!r}")
    return path


def parse_manifest_text(text: str) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip("\r\n")
        if not line.strip():
            continue
        if len(line) < 66 or not SHA_RE.fullmatch(line[:64]) or line[64] != " ":
            raise ColdBankError(EXIT_FAIL, f"invalid manifest line {lineno}")
        rel = line[66:]
        if rel.startswith("*"):
            rel = rel[1:]
        validate_rel(rel)
        digest = line[:64].lower()
        if rel in result and result[rel] != digest:
            raise ColdBankError(EXIT_FAIL, f"duplicate manifest path: {rel}")
        result[rel] = digest
    return result


def parse_manifest(path: str) -> Dict[str, str]:
    with open(path, encoding="utf-8") as fh:
        return parse_manifest_text(fh.read())


def _write_manifest(path: str, values: Mapping[str, str]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for rel in sorted(values):
            fh.write(f"{values[rel]}  {rel}\n")


def _parse_readme_text(text: str) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip() or "=" not in line:      # free-text lines (older hand-written READMEs) are kept out of meta
            continue
        key, value = line.split("=", 1)
        if key.strip() and " " not in key.strip():
            result[key.strip()] = value.strip()
    return result


def _read_readme(path: str) -> Dict[str, str]:
    with open(path, encoding="utf-8") as fh:
        return _parse_readme_text(fh.read())


def lease_live(
    lease: Mapping[str, Any],
    now: float,
    this_host: str,
    pid_alive: Callable[[int], bool],
) -> bool:
    """Return whether a lease protects an entry at ``now``."""
    try:
        if float(lease.get("expires", 0)) <= now:
            return False
    except (TypeError, ValueError):
        return False

    if lease.get("job"):
        return True

    if _short_host(str(lease.get("host", ""))) != _short_host(this_host):
        return True

    try:
        pid = int(lease.get("pid", 0))
    except (TypeError, ValueError):
        return False
    if pid == 0:                     # time lease (plain `fetch`): live until it expires or is released
        return True
    return pid_alive(pid)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


def split_streams(files: Any, n: int) -> List[List[Tuple[str, int]]]:
    """Balance files by bytes over ``n`` streams."""
    if n <= 0:
        raise ValueError("stream count must be positive")

    if isinstance(files, Mapping):
        pairs = [(str(path), int(size)) for path, size in files.items()]
    else:
        pairs = []
        for item in files:
            if isinstance(item, (tuple, list)) and len(item) == 2:
                pairs.append((str(item[0]), int(item[1])))
            else:
                path = str(item)
                pairs.append((path, os.stat(path).st_size))

    streams: List[List[Tuple[str, int]]] = [[] for _ in range(n)]
    totals = [0] * n
    for path, size in sorted(pairs, key=lambda pair: (-pair[1], pair[0])):
        target = min(range(n), key=lambda index: totals[index])
        streams[target].append((path, size))
        totals[target] += size
    return streams


def plan_fetch(
    name: str,
    files: Mapping[str, int],
    present: Iterable[str],
    entries: Mapping[str, Mapping[str, Any]],
    budget: int,
    free: int,
    leases: Mapping[str, Any],
    now: Optional[float] = None,
) -> FetchPlan:
    """Purely compute files to download and entries to evict."""
    validate_name(name)
    present_set = set(present)
    need = tuple(sorted(path for path in files if path not in present_set))
    need_bytes = sum(max(0, int(files[path])) for path in need)
    used = sum(max(0, int(entry.get("bytes", 0))) for entry in entries.values())
    original_used = used
    available = int(free)
    threshold = int(math.ceil(need_bytes * 1.05))
    evict: List[str] = []

    candidates = []
    for item, entry in entries.items():
        if item == name or leases.get(item):
            continue
        try:
            last_used = float(entry.get("last_used", 0))
        except (TypeError, ValueError):
            last_used = 0
        candidates.append((last_used, item, entry))

    candidates.sort(key=lambda row: (row[0], row[1]))
    for _, item, entry in candidates:
        if used + need_bytes <= budget and available >= threshold:
            break
        amount = max(0, int(entry.get("bytes", 0)))
        used -= amount
        available += amount
        evict.append(item)

    fits = used + need_bytes <= budget and available >= threshold
    reason = "" if fits else (
        f"no room: used={used} need={need_bytes} budget={budget} free={available}"
    )
    return FetchPlan(
        name=name,
        need=need,
        need_bytes=need_bytes,
        evict=tuple(evict),
        used_bytes=original_used,
        budget_bytes=int(budget),
        free_bytes=int(free),
        fits=fits,
        reason=reason,
    )


def _hash_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _hashes_under(root: str) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for base, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(base, d)))
        for name in sorted(names):
            path = os.path.join(base, name)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            result[rel] = _hash_file(path)
    return result


def _file_sizes(root: str) -> Dict[str, int]:
    result: Dict[str, int] = {}
    for base, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(base, d)))
        for name in sorted(names):
            path = os.path.join(base, name)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            if rel not in ("MANIFEST.sha256", "CONTENTS.sha256", "README.txt"):
                result[rel] = os.stat(path).st_size
    return result


SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "-c", "aes128-gcm@openssh.com"]   # fast cipher: 260 vs 152 MB/s
RSYNC_SSH = "ssh " + " ".join(SSH_OPTS)


def _ssh_argv(host: str, args: Sequence[str]) -> List[str]:
    remote = " ".join(shlex.quote(str(arg)) for arg in args)
    return ["ssh", *SSH_OPTS, host, remote]


def _ssh_script(host: str, script: str) -> str:
    """Run a remote shell snippet (every interpolated path must already be shlex.quote'd)."""
    try:
        result = subprocess.run(["ssh", *SSH_OPTS, host, script], capture_output=True, text=True)
    except OSError as exc:
        raise ColdBankError(EXIT_FAIL, f"ssh failed: {exc}") from exc
    if result.returncode:
        raise ColdBankError(EXIT_FAIL, f"ssh failed rc={result.returncode}: {result.stderr.strip()[-500:]}")
    return result.stdout


def _ssh_text(host: str, args: Sequence[str]) -> str:
    try:
        result = subprocess.run(_ssh_argv(host, args), capture_output=True, text=True)
    except OSError as exc:
        raise ColdBankError(EXIT_FAIL, f"ssh failed: {exc}") from exc
    if result.returncode:
        raise ColdBankError(EXIT_FAIL, f"ssh failed rc={result.returncode}: {result.stderr.strip()[-500:]}")
    return result.stdout


def _ssh_status(host: str, args: Sequence[str]) -> int:
    try:
        return subprocess.run(_ssh_argv(host, args), stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode
    except OSError:
        return 255


def _bank_item(host: Optional[str], bank: str, local: bool, name: str) -> BankItem:
    validate_name(name)

    if local:
        root = os.path.join(bank, name)
        try:
            manifest = parse_manifest(os.path.join(root, "MANIFEST.sha256"))
            meta = _read_readme(os.path.join(root, "README.txt"))
        except FileNotFoundError as exc:
            raise ColdBankError(EXIT_FAIL, f"incomplete bank item {name}: {exc}") from exc

        compressed = os.path.isfile(os.path.join(root, "CONTENTS.sha256"))
        contents = parse_manifest(os.path.join(root, "CONTENTS.sha256")) if compressed else {}
        files = contents if compressed else manifest
        sizes = _file_sizes(root)

        archive = None
        if compressed:
            archive = os.path.join(root, "data.tar.zst")
            if not os.path.isfile(archive):
                raise ColdBankError(EXIT_FAIL, f"compressed item {name} has no data.tar.zst")
            sizes = {path: 0 for path in files}
            sizes["data.tar.zst"] = os.path.getsize(archive)

        return BankItem(name, root, manifest, files, sizes, meta, compressed, archive)

    root = bank.rstrip("/") + "/" + name
    q = shlex.quote(root)
    mark = "@@COLDBANK@@"                 # one ssh round trip (westeros is often loaded): manifest, README, contents, sizes
    raw = _ssh_script(host or "", (
        f"cd {q} || exit 3; for f in MANIFEST.sha256 README.txt CONTENTS.sha256; do echo; echo {mark}$f; "
        f"[ -f $f ] && cat $f; done; echo; echo {mark}LIST; find . -type f -printf '%P\\t%s\\n'"))
    parts: Dict[str, str] = {}
    key = None
    for line in raw.splitlines(keepends=True):
        if line.startswith(mark):
            key = line[len(mark):].strip()
            parts[key] = ""
        elif key is not None:
            parts[key] += line
    if not parts.get("MANIFEST.sha256", "").strip():
        raise ColdBankError(EXIT_FAIL, f"incomplete bank item {name}: no MANIFEST.sha256")
    manifest = parse_manifest_text(parts["MANIFEST.sha256"])
    meta = _parse_readme_text(parts.get("README.txt", ""))
    compressed = "data.tar.zst" in manifest
    contents = parse_manifest_text(parts.get("CONTENTS.sha256", "")) if compressed else {}
    files = contents if compressed else manifest

    listing = parts.get("LIST", "")
    sizes: Dict[str, int] = {}
    for line in listing.splitlines():
        rel, sep, raw_size = line.rpartition("\t")
        if sep and raw_size.isdigit():
            sizes[rel] = int(raw_size)

    archive = root + "/data.tar.zst" if compressed else None
    if compressed:
        sizes = {path: 0 for path in files}
        sizes["data.tar.zst"] = next(
            (int(line.rpartition("\t")[2]) for line in listing.splitlines()
             if line.rpartition("\t")[0] == "data.tar.zst"
             and line.rpartition("\t")[2].isdigit()),
            0,
        )

    return BankItem(name, root, manifest, files, sizes, meta, compressed, archive)


def _cache_entry(path: str) -> Optional[Dict[str, Any]]:
    if os.path.islink(path) or not os.path.isdir(path):
        return None
    try:
        with open(os.path.join(path, ".entry.json"), encoding="utf-8") as fh:
            entry = json.load(fh)
    except (OSError, ValueError):
        return None
    return entry if isinstance(entry, dict) and isinstance(entry.get("files"), dict) else None


def _cache_entries(cache: str) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    if not os.path.isdir(cache):
        return result
    for name in os.listdir(cache):
        if name.startswith(".") or not NAME_RE.fullmatch(name):
            continue
        entry = _cache_entry(os.path.join(cache, name))
        if entry is not None and entry.get("name", name) == name:
            result[name] = entry
    return result


def _lease_dir(cache: str, name: str) -> str:
    return os.path.join(cache, ".leases", name)


def _live_leases(cache: str, name: str, now: Optional[float] = None) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    directory = _lease_dir(cache, name)
    if not os.path.isdir(directory):
        return result

    stamp = time.time() if now is None else now
    for filename in os.listdir(directory):
        if not filename.endswith(".json"):
            continue
        path = os.path.join(directory, filename)
        try:
            with open(path, encoding="utf-8") as fh:
                lease = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(lease, dict) and lease_live(lease, stamp, _this_host(), _pid_alive):
            result[str(lease.get("id", filename))] = lease
    return result


@contextmanager
def _flock(path: str) -> Iterator[None]:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o640)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _log(cache: str, event: str, **values: Any) -> None:
    os.makedirs(cache, exist_ok=True)
    record = {"event": event, "time": time.time(), **values}
    with open(os.path.join(cache, "coldcache.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def _quota_room(path: str) -> Optional[int]:
    """Bytes left under this user's block quota on the filesystem holding ``path`` (None: no quota / unknown).
    Parses `quota -w -v` (blocks, soft, hard in KiB); the smaller nonzero limit counts (ilabu: soft 15 GB > hard 10 GB)."""
    try:
        src = subprocess.run(["findmnt", "-no", "SOURCE", "--target", path], capture_output=True, text=True, timeout=20).stdout.strip()
        out = subprocess.run(["quota", "-w", "-v"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        cols = line.split()
        if len(cols) >= 4 and src and cols[0] == src:
            try:
                used = int(cols[1].rstrip("*"))
                limits = [int(c) for c in cols[2:4] if int(c) > 0]
            except ValueError:
                return None
            return max(0, (min(limits) - used) * 1024) if limits else None
    return None


def _free_bytes(cache: str) -> int:
    probe = cache if os.path.exists(cache) else os.path.dirname(os.path.abspath(cache))
    try:
        statvfs = os.statvfs(probe)
        free = int(statvfs.f_bavail * statvfs.f_frsize)
    except OSError:
        return 0
    room = _quota_room(probe)
    return free if room is None else min(free, room)


def _cache_settings(args: argparse.Namespace, host: Optional[str] = None) -> Tuple[str, int]:
    """Cache dir + budget of THIS host (``host`` is the bank host and is ignored: the cache is always local)."""
    settings = host_settings(_this_host(), args.config)
    cache = args.cache or os.environ.get("CDX_COLDCACHE") or settings.get("cache")
    if not cache:
        raise ColdBankError(EXIT_REFUSED, f"no cache configured for host {_this_host()}")

    raw_budget = args.budget_gb
    if raw_budget is None:
        raw_budget = settings.get("budget_gb")
    if raw_budget is None:
        raise ColdBankError(EXIT_REFUSED, "no cache budget configured; use --budget-gb")

    try:
        budget = int(float(raw_budget) * 1024 ** 3)
    except (TypeError, ValueError) as exc:
        raise ColdBankError(EXIT_REFUSED, f"invalid cache budget: {raw_budget!r}") from exc
    if budget < 0:
        raise ColdBankError(EXIT_REFUSED, "cache budget must not be negative")

    return os.path.abspath(os.path.expanduser(str(cache))), budget


def _parse_meta(values: Sequence[str]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ColdBankError(EXIT_REFUSED, f"metadata must be key=value: {value!r}")
        key, item = value.split("=", 1)
        if not key:
            raise ColdBankError(EXIT_REFUSED, "metadata key is empty")
        result[key] = item

    missing = [key for key in REQUIRED_META if not result.get(key)]
    if missing:
        raise ColdBankError(EXIT_REFUSED, "missing required metadata: " + ", ".join(missing))
    if result["regenerable"] not in ("yes", "no"):
        raise ColdBankError(EXIT_REFUSED, "regenerable must be yes or no")
    if any("\n" in key or "\n" in value or "\r" in key or "\r" in value
           for key, value in result.items()):
        raise ColdBankError(EXIT_REFUSED, "metadata may not contain newlines")
    return result


def _copy_source(source: str, destination: str) -> None:
    source = os.path.abspath(os.path.expanduser(source))
    if os.path.islink(source) or not os.path.exists(source):
        raise ColdBankError(EXIT_REFUSED, f"source is missing or a symlink: {source}")

    if os.path.isdir(source):
        for base, dirs, names in os.walk(source, followlinks=False):
            if any(os.path.islink(os.path.join(base, name)) for name in dirs + names):
                raise ColdBankError(EXIT_REFUSED, "source contains a symlink")
            rel = os.path.relpath(base, source)
            target = destination if rel == "." else os.path.join(destination, rel)
            os.makedirs(target, exist_ok=True)
            for name in names:
                shutil.copy2(os.path.join(base, name), os.path.join(target, name))
    else:
        os.makedirs(destination, exist_ok=True)
        shutil.copy2(source, os.path.join(destination, os.path.basename(source)))


def _make_archive(content: str, archive: str) -> None:
    if not shutil.which("tar") or not shutil.which("zstd"):
        raise ColdBankError(EXIT_REFUSED, "--zstd requires tar and zstd")

    with open(archive, "wb") as output:
        tar_proc = subprocess.Popen(
            ["tar", "-C", content, "-cf", "-", "."],
            stdout=subprocess.PIPE,
        )
        assert tar_proc.stdout is not None
        zstd_proc = subprocess.Popen(
            ["zstd", "-q", "-c"],
            stdin=tar_proc.stdout,
            stdout=output,
        )
        tar_proc.stdout.close()
        zstd_rc = zstd_proc.wait()
        tar_rc = tar_proc.wait()

    if tar_rc or zstd_rc:
        raise ColdBankError(EXIT_FAIL, f"archive creation failed tar={tar_rc} zstd={zstd_rc}")


def _make_stage(
    source: str,
    name: str,
    meta: Mapping[str, str],
    compressed: bool,
    parent: str,
) -> str:
    stage = tempfile.mkdtemp(prefix=f".{name}.stage.", dir=parent)
    content = os.path.join(stage, "content")
    try:
        _copy_source(source, content)
        contents = _hashes_under(content)

        if compressed:
            archive = os.path.join(stage, "data.tar.zst")
            _make_archive(content, archive)
            _write_manifest(os.path.join(stage, "CONTENTS.sha256"), contents)
            _write_manifest(
                os.path.join(stage, "MANIFEST.sha256"),
                {"data.tar.zst": _hash_file(archive)},
            )
        else:
            for rel in contents:
                target = os.path.join(stage, rel)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                os.replace(os.path.join(content, rel), target)
            _write_manifest(os.path.join(stage, "MANIFEST.sha256"), contents)

        shutil.rmtree(content, ignore_errors=True)
        with open(os.path.join(stage, "README.txt"), "w", encoding="utf-8") as fh:
            for key in REQUIRED_META:
                fh.write(f"{key}={meta[key]}\n")
            for key in sorted(set(meta) - set(REQUIRED_META)):
                fh.write(f"{key}={meta[key]}\n")
        return stage
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def _verify_local_item(item: str) -> bool:
    if shutil.which("sha256sum"):
        try:
            return subprocess.run(
                ["sha256sum", "-c", "--quiet", "MANIFEST.sha256"],
                cwd=item,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode == 0
        except OSError:
            return False

    try:
        manifest = parse_manifest(os.path.join(item, "MANIFEST.sha256"))
        return all(
            os.path.isfile(os.path.join(item, *rel.split("/")))
            and _hash_file(os.path.join(item, *rel.split("/"))) == digest
            for rel, digest in manifest.items()
        )
    except (OSError, ColdBankError):
        return False


def _verify_remote_item(host: str, item: str) -> bool:
    command = (
        f"cd {shlex.quote(item)} && "
        f"sha256sum -c --quiet {shlex.quote('MANIFEST.sha256')}"
    )
    try:
        return subprocess.run(
            ["ssh", *SSH_OPTS, host, command],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode == 0
    except OSError:
        return False


def _put(args: argparse.Namespace) -> int:
    name = validate_name(args.name)
    meta = _parse_meta(args.meta)
    host, bank, local = parse_bank(args.bank)

    if local:
        os.makedirs(bank, exist_ok=True)
        item = os.path.join(bank, name)
        if os.path.exists(item) and not args.update:
            raise ColdBankError(EXIT_REFUSED, f"bank item already exists: {name}")

        stage = _make_stage(args.source, name, meta, args.zstd, bank)
        if not _verify_local_item(stage):                   # verify BEFORE it becomes the bank copy
            shutil.rmtree(stage, ignore_errors=True)        # our own staging dir, never a bank item
            raise ColdBankError(EXIT_FAIL, f"bank verification failed for {name}")
        old = None
        if os.path.exists(item):                            # --update: the old version is kept (rule: move, never delete)
            old = f"{item}.old.{time.strftime('%Y%m%d_%H%M%S')}"
            os.replace(item, old)
        os.replace(stage, item)
        print(f"put {name}")
        if old:
            print(f"old version kept: {old}  (remove it yourself when no longer needed: ! rm -rf {shlex.quote(old)})")
        return EXIT_OK

    if not shutil.which("rsync"):
        raise ColdBankError(EXIT_REFUSED, "rsync is required for a remote bank")

    temporary = tempfile.mkdtemp(prefix=f"coldbank-{name}-")
    try:
        remote_item = bank.rstrip("/") + "/" + name
        exists = _ssh_status(host or "", ["test", "-e", remote_item]) == 0
        if exists and not args.update:
            raise ColdBankError(EXIT_REFUSED, f"bank item already exists: {name}")

        stage = _make_stage(args.source, name, meta, args.zstd, temporary)
        incoming = bank.rstrip("/") + f"/.{name}.incoming.{_this_host()}.{os.getpid()}"
        _ssh_text(host or "", ["mkdir", "-p", incoming])
        result = subprocess.run([
            "rsync", "-a",
            "-e", RSYNC_SSH,
            stage + "/", f"{host}:{incoming}/",
        ])
        if result.returncode:
            raise ColdBankError(EXIT_FAIL, f"rsync failed rc={result.returncode}; partial upload left at {host}:{incoming}")
        if not _verify_remote_item(host or "", incoming):
            raise ColdBankError(EXIT_FAIL, f"bank verification failed for {name}; upload left at {host}:{incoming}")
        old = f"{remote_item}.old.{time.strftime('%Y%m%d_%H%M%S')}"
        q = shlex.quote
        out = _ssh_script(host or "", (
            f"if [ -e {q(remote_item)} ]; then "
            f"[ {'1' if args.update else '0'} = 1 ] || {{ echo EXISTS; exit 0; }}; mv {q(remote_item)} {q(old)}; echo OLD; fi; "
            f"mv {q(incoming)} {q(remote_item)} && echo DONE"))
        if "EXISTS" in out:
            raise ColdBankError(EXIT_REFUSED, f"bank item appeared during upload: {name}; upload left at {host}:{incoming}")
        print(f"put {name}")
        if "OLD" in out:
            print(f"old version kept: {host}:{old}  (remove it yourself when no longer needed)")
        return EXIT_OK
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def _list(args: argparse.Namespace) -> int:
    host, bank, local = parse_bank(args.bank)

    if not local:
        q = shlex.quote(bank.rstrip("/"))
        listing = _ssh_script(host or "", (
            f"cd {q} && find . -mindepth 2 -type f -printf 'F\\t%P\\t%s\\n'; "
            f"for r in */README.txt; do [ -f \"$r\" ] && grep -E '^(source|regenerable|cost)=' \"$r\" | sed \"s|^|M\\t${{r%/README.txt}}\\t|\"; done; true"))
        totals: Dict[str, int] = {}
        counts: Dict[str, int] = {}
        meta: Dict[str, Dict[str, str]] = {}
        for line in listing.splitlines():
            kind, _, rest = line.partition("\t")
            if kind == "F":
                rel, _, raw_size = rest.rpartition("\t")
                name = rel.split("/", 1)[0]
                if name.startswith(".") or not NAME_RE.fullmatch(name) or not raw_size.isdigit():
                    continue
                if rel.split("/", 1)[-1] in ("MANIFEST.sha256", "CONTENTS.sha256", "README.txt"):
                    continue
                totals[name] = totals.get(name, 0) + int(raw_size)
                counts[name] = counts.get(name, 0) + 1
            elif kind == "M":
                name, _, kv = rest.partition("\t")
                key, _, value = kv.partition("=")
                meta.setdefault(name, {})[key] = value.strip()
        for name in sorted(totals):
            m = meta.get(name, {})
            print(f"{name}\tfiles={counts[name]}\tbytes={totals[name]}"
                  f"\tsource={m.get('source', '')}\tregenerable={m.get('regenerable', '')}\tcost={m.get('cost', '')}")
        return EXIT_OK

    if not os.path.isdir(bank):
        raise ColdBankError(EXIT_FAIL, f"bank does not exist: {bank}")

    for name in sorted(os.listdir(bank)):
        item = os.path.join(bank, name)
        if name.startswith(".") or not NAME_RE.fullmatch(name) or not os.path.isdir(item):
            continue
        try:
            info = _bank_item(None, bank, True, name)
            if info.compressed:
                total = os.path.getsize(info.archive or "")
            else:
                total = sum(info.sizes.get(path, 0) for path in info.files)
            print(
                f"{name}\tfiles={len(info.files)}\tbytes={total}"
                f"\tcompressed={'yes' if info.compressed else 'no'}"
                f"\tsource={info.meta.get('source', '')}"
                f"\tregenerable={info.meta.get('regenerable', '')}"
                f"\tcost={info.meta.get('cost', '')}"
            )
        except ColdBankError as exc:
            print(f"{name}\tINVALID\t{exc}")
    return EXIT_OK


def _present_files(final: str, expected: Mapping[str, str], rehash: bool = False) -> List[str]:
    """Files of ``expected`` already in the cache entry. A file verified at fetch time (sha + size recorded in .entry.json)
    is trusted while its size is unchanged; others (or all, with ``rehash``) are hashed."""
    entry = _cache_entry(final) or {}
    known, known_size = entry.get("files", {}), entry.get("sizes", {})
    present = []
    for rel, digest in expected.items():
        path = os.path.join(final, *rel.split("/"))
        try:
            if not os.path.isfile(path) or os.path.islink(path):
                continue
            if not rehash and known.get(rel) == digest and known_size.get(rel) == os.path.getsize(path):
                present.append(rel)
            elif _hash_file(path) == digest:
                present.append(rel)
        except OSError:
            pass
    return present


def _remove_entry(cache: str, name: str) -> None:
    path = os.path.join(cache, name)
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)


def _copy_local(source: str, partial: str, rel: str) -> None:
    target = os.path.join(partial, *rel.split("/"))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.copy2(os.path.join(source, *rel.split("/")), target)


def _rsync_stream(
    host: str,
    root: str,
    partial: str,
    paths: Sequence[str],
    index: int,
) -> None:
    if not paths:
        return

    list_path = os.path.join(partial, f".files.{index}")
    try:
        with open(list_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(paths) + "\n")
        result = subprocess.run([
            "rsync", "-a", "--files-from=" + list_path,
            "-e", RSYNC_SSH,
            f"{host}:{root.rstrip('/')}/", partial + "/",
        ], capture_output=True, text=True)
    finally:
        try:
            os.unlink(list_path)
        except OSError:
            pass

    if result.returncode:
        raise ColdBankError(EXIT_FAIL, f"rsync stream {index} failed rc={result.returncode}")


def _download_normal(
    info: BankItem,
    partial: str,
    needed: Sequence[str],
    streams: int,
    local: bool,
    host: Optional[str],
) -> None:
    pairs = [(rel, info.sizes.get(rel, 0)) for rel in needed]
    groups = split_streams(pairs, max(1, streams))

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, streams)) as pool:
        if local:
            futures = [
                pool.submit(_copy_local, info.root, partial, rel)
                for group in groups
                for rel, _ in group
            ]
        else:
            futures = [
                pool.submit(
                    _rsync_stream,
                    host or "",
                    info.root,
                    partial,
                    [rel for rel, _ in group],
                    index,
                )
                for index, group in enumerate(groups)
            ]
        for future in futures:
            try:
                future.result()
            except OSError as exc:
                raise ColdBankError(EXIT_FAIL, f"copy failed: {exc}") from exc


def _extract_zstd(
    info: BankItem,
    partial: str,
    needed: Sequence[str],
    local: bool,
    host: Optional[str],
) -> None:
    if not shutil.which("tar") or not shutil.which("zstd"):
        raise ColdBankError(EXIT_REFUSED, "compressed fetch requires tar and zstd")

    archive_fh = None
    ssh_proc = None
    try:
        if local:
            archive_fh = open(info.archive or "", "rb")
            source = archive_fh
        else:
            ssh_proc = subprocess.Popen(
                _ssh_argv(host or "", ["cat", info.archive or ""]),
                stdout=subprocess.PIPE,
            )
            assert ssh_proc.stdout is not None
            source = ssh_proc.stdout

        zstd_proc = subprocess.Popen(
            ["zstd", "-q", "-d", "-c"],
            stdin=source,
            stdout=subprocess.PIPE,
        )
        if ssh_proc is not None:
            source.close()

        assert zstd_proc.stdout is not None
        tar_command = ["tar", "-x", "-f", "-", "-C", partial]
        if needed and len(needed) != len(info.files):
            tar_command += ["--"] + ["./" + rel for rel in needed]

        tar_proc = subprocess.Popen(tar_command, stdin=zstd_proc.stdout)
        zstd_proc.stdout.close()
        tar_rc = tar_proc.wait()
        zstd_rc = zstd_proc.wait()
        ssh_rc = ssh_proc.wait() if ssh_proc is not None else 0

        if tar_rc or zstd_rc or ssh_rc:
            raise ColdBankError(
                EXIT_FAIL,
                f"archive extraction failed tar={tar_rc} zstd={zstd_rc} ssh={ssh_rc}",
            )
    except OSError as exc:
        raise ColdBankError(EXIT_FAIL, f"archive extraction failed: {exc}") from exc
    finally:
        if archive_fh is not None:
            archive_fh.close()
        if ssh_proc is not None and ssh_proc.poll() is None:
            ssh_proc.kill()
            ssh_proc.wait()


def _write_json(path: str, value: Mapping[str, Any]) -> None:
    temporary = path + f".tmp.{os.getpid()}"
    with open(temporary, "w", encoding="utf-8") as fh:
        json.dump(dict(value), fh, sort_keys=True)
        fh.write("\n")
    os.replace(temporary, path)


def _new_lease(cache: str, name: str, hours: float, job: Optional[str], pid: Optional[int] = None) -> str:
    """``pid`` = the process the lease protects (fetch: the caller, i.e. our parent; run: this process)."""
    pid = os.getpid() if pid is None else int(pid)
    identifier = f"{_this_host()}-{pid}-{time.time_ns()}"
    lease = {
        "id": identifier,
        "pid": pid,
        "host": _this_host(),
        "job": job or "",
        "created": time.time(),
        "expires": time.time() + max(0.0, hours) * 3600,
    }
    directory = os.path.join(cache, ".leases", name)
    os.makedirs(directory, exist_ok=True)
    _write_json(os.path.join(directory, identifier + ".json"), lease)
    return identifier


def _release_lease(cache: str, name: str, identifier: Optional[str], pid: Optional[int] = None) -> None:
    directory = os.path.join(cache, ".leases", name)
    if not os.path.isdir(directory):
        return

    for filename in os.listdir(directory):
        if not filename.endswith(".json"):
            continue
        path = os.path.join(directory, filename)
        try:
            with open(path, encoding="utf-8") as fh:
                lease = json.load(fh)
        except (OSError, ValueError):
            continue

        matches = identifier and lease.get("id") == identifier
        self_lease = (
            not identifier
            and int(lease.get("pid", -1)) == (os.getpid() if pid is None else int(pid))
            and _short_host(str(lease.get("host", ""))) == _this_host()
        )
        if not identifier and pid == -1:              # release NAME --all: every lease of NAME taken on this host
            self_lease = _short_host(str(lease.get("host", ""))) == _this_host()
        if matches or self_lease:
            try:
                os.unlink(path)
            except OSError:
                pass


def _fetch(args: argparse.Namespace, announce: bool = True) -> Tuple[str, Optional[str]]:
    name = validate_name(args.name)
    host, bank, local = parse_bank(args.bank)
    info = _bank_item(host, bank, local, name)

    if host and local:
        path = os.path.join(bank, name)
        if announce:
            print(path)
        return path, None

    cache, budget = _cache_settings(args, host)
    selected = [rel for group in args.only for rel in group]
    for rel in selected:
        validate_rel(rel)
        if rel not in info.files:
            raise ColdBankError(EXIT_REFUSED, f"--only path is not in the manifest: {rel}")

    requested = selected or list(info.files)
    expected = {rel: info.files[rel] for rel in requested}
    sizes = {rel: info.sizes.get(rel, 0) for rel in requested}

    if not args.dry_run:
        os.makedirs(cache, exist_ok=True)

    lock_path = os.path.join(cache, ".locks", name + ".lock")
    # one fetch at a time per cache: planning, eviction and download all happen under the cache lock, so two fetches
    # can never both count the same free bytes, and an entry is never evicted while another process fills it
    with _flock(lock_path), _flock(os.path.join(cache, ".lock")):
        final = os.path.join(cache, name)
        present = _present_files(final, expected, getattr(args, "rehash", False))
        entries = _cache_entries(cache)
        leases = {
            item: bool(_live_leases(cache, item))
            for item in entries
        }
        plan = plan_fetch(
            name,
            sizes,
            present,
            entries,
            budget,
            _free_bytes(cache),
            leases,
        )

        if args.dry_run:
            print(f"need {len(plan.need)} file(s), {plan.need_bytes} bytes")
            if plan.evict:
                print("evict: " + " ".join(plan.evict))
            if not plan.fits:
                print(plan.reason, file=sys.stderr)
                return "", None
            return final, None

        if not plan.fits:
            held = [item for item, live in leases.items() if live]
            suffix = f"; live leases: {', '.join(sorted(held))}" if held else ""
            raise ColdBankError(EXIT_NO_ROOM, plan.reason + suffix)

        for item in plan.evict:
            _remove_entry(cache, item)
            _log(cache, "evict", name=item, reason="admission", for_item=name)

        partial_root = os.path.join(cache, ".partial")
        os.makedirs(partial_root, exist_ok=True)
        partial = tempfile.mkdtemp(prefix=f"{name}.{os.getpid()}.", dir=partial_root)

        try:
            if plan.need:
                if info.compressed:
                    _extract_zstd(info, partial, plan.need, local, host)
                else:
                    _download_normal(
                        info,
                        partial,
                        plan.need,
                        max(1, args.streams),
                        local,
                        host,
                    )

                for rel in plan.need:
                    path = os.path.join(partial, *rel.split("/"))
                    if not os.path.isfile(path) or _hash_file(path) != expected[rel]:
                        raise ColdBankError(EXIT_FAIL, f"checksum mismatch for {name}/{rel}")

            os.makedirs(final, exist_ok=True)
            for rel in plan.need:
                source = os.path.join(partial, *rel.split("/"))
                target = os.path.join(final, *rel.split("/"))
                os.makedirs(os.path.dirname(target), exist_ok=True)
                os.replace(source, target)

            old = _cache_entry(final) or {}
            files = dict(old.get("files", {}))
            files.update(expected)
            file_sizes = {
                rel: os.path.getsize(os.path.join(final, *rel.split("/")))
                for rel in files
                if os.path.isfile(os.path.join(final, *rel.split("/")))
            }
            files = {rel: files[rel] for rel in file_sizes}
            bytes_used = sum(file_sizes.values())
            _write_json(
                os.path.join(final, ".entry.json"),
                {
                    "name": name,
                    "bank": args.bank,
                    "files": files,
                    "sizes": file_sizes,
                    "bytes": bytes_used,
                    "created": old.get("created", time.time()),
                    "last_used": time.time(),
                },
            )
        except Exception:
            shutil.rmtree(partial, ignore_errors=True)
            raise
        finally:
            shutil.rmtree(partial, ignore_errors=True)

        if plan.need:
            _log(cache, "fetch", name=name, files=len(plan.need), bytes=plan.need_bytes)
        lease_id = None
        if not args.no_lease:
            # run: a pid lease on this process (dies with it); fetch: a time lease (pid 0) unless --pid names the holder --
            # the caller of `fetch` is usually a short-lived wrapper/subshell, so its pid would expire the lease at once
            holder = args.pid if args.pid else (os.getpid() if args.command == "run" else 0)
            lease_id = _new_lease(cache, name, args.lease_hours, args.job or os.environ.get("SLURM_JOB_ID"), holder)
            print(f"lease {lease_id}", file=sys.stderr)
        if announce:
            print(final)
        return final, lease_id


def _run(args: argparse.Namespace) -> int:
    command = list(args.cmd)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        raise ColdBankError(EXIT_REFUSED, "run requires -- CMD...")

    path, lease_id = _fetch(args, announce=False)
    if not path:
        return EXIT_NO_ROOM

    host, _, local = parse_bank(args.bank)
    cache = None if host and local else _cache_settings(args, host)[0]
    env = dict(os.environ)
    env["COLDBANK_PATH"] = path
    old_handlers: Dict[int, Any] = {}

    def release_signal(signum: int, frame: Any) -> None:
        if cache and lease_id:
            _release_lease(cache, args.name, lease_id)
        raise SystemExit(128 + signum)

    try:
        if cache and lease_id:
            for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                old_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, release_signal)
        try:
            return subprocess.run(command, env=env).returncode
        finally:
            if cache and lease_id:
                _release_lease(cache, args.name, lease_id)
    finally:
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)


def _cat(args: argparse.Namespace) -> int:
    name = validate_name(args.name)
    rel = validate_rel(args.rel)
    host, bank, local = parse_bank(args.bank)
    info = _bank_item(host, bank, local, name)

    if rel not in info.manifest:
        raise ColdBankError(EXIT_REFUSED, f"path is not in the bank manifest: {rel}")

    digest = hashlib.sha256()
    source_fh = None
    source_proc = None

    if local:
        source_fh = open(os.path.join(info.root, *rel.split("/")), "rb")
        source = source_fh
    else:
        source_proc = subprocess.Popen(
            _ssh_argv(host or "", ["cat", info.root.rstrip("/") + "/" + rel]),
            stdout=subprocess.PIPE,
        )
        assert source_proc.stdout is not None
        source = source_proc.stdout

    output = getattr(sys.stdout, "buffer", sys.stdout)
    try:
        while True:
            block = source.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
            try:
                output.write(block)
            except TypeError:
                sys.stdout.write(block.decode("utf-8", "replace"))
        output.flush()
        source_rc = source_proc.wait() if source_proc is not None else 0
    finally:
        if source_fh is not None:
            source_fh.close()
        if source_proc is not None and source_proc.poll() is None:
            source_proc.kill()
            source_proc.wait()

    if source_rc:
        raise ColdBankError(EXIT_FAIL, f"bank read failed rc={source_rc}")
    if digest.hexdigest() != info.manifest[rel]:
        print(f"checksum mismatch for {name}/{rel}", file=sys.stderr)
        return EXIT_FAIL
    return EXIT_OK


def _release(args: argparse.Namespace) -> int:
    validate_name(args.name)
    host, _, local = parse_bank(args.bank)
    if host and local:
        return EXIT_OK
    cache, _ = _cache_settings(args, host)
    _release_lease(cache, args.name, args.identifier, -1 if args.all else (args.pid if args.pid else 0))
    _log(cache, "release", name=args.name, id=args.identifier or "")
    return EXIT_OK


def _status(args: argparse.Namespace) -> int:
    host, _, local = parse_bank(args.bank)
    if host and local:
        print("bank host: no local cache")
        return EXIT_OK

    cache, budget = _cache_settings(args, host)
    entries = _cache_entries(cache)
    used = sum(int(entry.get("bytes", 0)) for entry in entries.values())
    print(f"cache={cache}\nbudget={budget}\nused={used}\nentries={len(entries)}")
    for name in sorted(entries):
        print(
            f"{name}\tbytes={entries[name].get('bytes', 0)}"
            f"\tlast_used={entries[name].get('last_used', 0)}"
            f"\tlive_leases={len(_live_leases(cache, name))}"
        )
    return EXIT_OK


def _gc(args: argparse.Namespace) -> int:
    host, _, local = parse_bank(args.bank)
    if host and local:
        return EXIT_OK

    cache, budget = _cache_settings(args, host)
    if not os.path.isdir(cache):
        return EXIT_OK

    with _flock(os.path.join(cache, ".lock")):
        return _gc_locked(args, cache, budget)


def _gc_locked(args: argparse.Namespace, cache: str, budget: int) -> int:
    now = time.time()
    leases_root = os.path.join(cache, ".leases")
    if os.path.isdir(leases_root):
        for name in os.listdir(leases_root):
            directory = os.path.join(leases_root, name)
            if not os.path.isdir(directory):
                continue
            for filename in os.listdir(directory):
                path = os.path.join(directory, filename)
                try:
                    with open(path, encoding="utf-8") as fh:
                        lease = json.load(fh)
                    if not isinstance(lease, dict) or not lease_live(
                        lease, now, _this_host(), _pid_alive
                    ):
                        os.unlink(path)
                except (OSError, ValueError):
                    try:
                        os.unlink(path)
                    except OSError:
                        pass

    partial_root = os.path.join(cache, ".partial")
    if os.path.isdir(partial_root):
        for name in os.listdir(partial_root):
            path = os.path.join(partial_root, name)
            try:
                if now - os.stat(path).st_mtime > 24 * 3600:
                    shutil.rmtree(path)
            except OSError:
                pass

    entries = _cache_entries(cache)
    leases = {name: bool(_live_leases(cache, name)) for name in entries}
    plan = plan_fetch("gc", {}, (), entries, budget, _free_bytes(cache), leases)
    for name in plan.evict:
        if args.dry_run:
            print(f"would evict {name}")
        else:
            _remove_entry(cache, name)
            _log(cache, "evict", name=name, reason="gc")
    return EXIT_OK


def _verify(args: argparse.Namespace) -> int:
    name = validate_name(args.name)
    host, bank, local = parse_bank(args.bank)
    info = _bank_item(host, bank, local, name)
    ok = _verify_local_item(info.root) if local else _verify_remote_item(host or "", info.root)
    if not ok:
        print(f"verification failed: {name}", file=sys.stderr)
        return EXIT_FAIL
    print(f"verified {name}")
    return EXIT_OK


def _add_common(parser: argparse.ArgumentParser, suppress: bool = False) -> None:
    default = argparse.SUPPRESS if suppress else None
    parser.add_argument("--bank", default=default)
    parser.add_argument("--cache", default=default)
    parser.add_argument("--budget-gb", type=float, default=default)
    parser.add_argument("--config", default=default)


def _add_fetch(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--only", nargs="+", action="append", default=[], metavar="REL")
    parser.add_argument("--streams", type=int, default=4)
    lease = parser.add_mutually_exclusive_group()
    lease.add_argument("--lease-hours", type=float, default=12)
    lease.add_argument("--no-lease", action="store_true")
    parser.add_argument("--job", help="Slurm job id the lease belongs to (default $SLURM_JOB_ID); live until it expires")
    parser.add_argument("--pid", type=int, help="process the lease protects (default: fetch = the caller, run = itself)")
    parser.add_argument("--rehash", action="store_true", help="re-hash cached files instead of trusting the entry record")
    parser.add_argument("--dry-run", action="store_true")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cdx-agent --coldbank",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    command = sub.add_parser("list")
    _add_common(command, True)

    command = sub.add_parser("put")
    _add_common(command, True)
    command.add_argument("source")
    command.add_argument("name")
    command.add_argument("--meta", action="append", default=[])
    command.add_argument("--zstd", action="store_true")
    command.add_argument("--update", action="store_true")

    command = sub.add_parser("fetch")
    _add_common(command, True)
    command.add_argument("name")
    _add_fetch(command)

    command = sub.add_parser("run")
    _add_common(command, True)
    command.add_argument("name")
    _add_fetch(command)
    command.add_argument("cmd", nargs=argparse.REMAINDER)

    command = sub.add_parser("release")
    _add_common(command, True)
    command.add_argument("name")
    command.add_argument("--id", dest="identifier")
    command.add_argument("--pid", type=int, help="release the pid leases of this pid (default: the time leases, pid 0)")
    command.add_argument("--all", action="store_true", help="release every lease of NAME taken on this host")

    command = sub.add_parser("cat")
    _add_common(command, True)
    command.add_argument("name")
    command.add_argument("rel")

    command = sub.add_parser("status")
    _add_common(command, True)

    command = sub.add_parser("gc")
    _add_common(command, True)
    command.add_argument("--dry-run", action="store_true")

    command = sub.add_parser("verify")
    _add_common(command, True)
    command.add_argument("name")

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
        if not args.bank:
            args.bank = os.environ.get("CDX_COLDBANK", DEFAULT_BANK)

        if args.command == "list":
            return _list(args)
        if args.command == "put":
            return _put(args)
        if args.command == "fetch":
            if args.streams <= 0:
                raise ColdBankError(EXIT_REFUSED, "--streams must be positive")
            path, _ = _fetch(args)
            return EXIT_OK if path else EXIT_NO_ROOM
        if args.command == "run":
            return _run(args)
        if args.command == "release":
            return _release(args)
        if args.command == "cat":
            return _cat(args)
        if args.command == "status":
            return _status(args)
        if args.command == "gc":
            return _gc(args)
        if args.command == "verify":
            return _verify(args)
        raise ColdBankError(EXIT_REFUSED, f"unknown command: {args.command}")
    except ColdBankError as exc:
        print(f"coldbank: {exc}", file=sys.stderr)
        return exc.code
    except (OSError, ValueError) as exc:
        print(f"coldbank: {exc}", file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
