"""Session lock acquisition, conflict diagnosis, and PID-verified cancellation.

Fixes the confirmed **A3** safety bug in the bash predecessor (`bin/cdx-agent`):
`cdx_cancel_active_session` matched "active" processes with
``ps -u $USER | grep -Ei 'codex|cdx-agent' | grep -F -e "$repo" -e "$runtime_dir"``
-- a substring match against the whole `ps` command-line text, filtered only by
excluding the caller's own PID/PPID. That heuristic can match, and therefore
TERM-then-KILL, *any* process (an editor, a `tail -f`, an unrelated shell)
whose argv merely happens to mention the repo path, and it never re-checked
whether the flock was actually still held by a matched PID before signaling
it. This already caused one incident (see
``codex_tools/active_session_cancel_fix_20260620_001637/cdx-agent.diff``).

This module replaces that with PID-verified cancellation: the owning PID is
written into the lock file at acquisition time, and cancellation only ever
signals that recorded PID (plus its direct children) after confirming it is
still alive *and* still the actual lock holder. If there's no verifiable live
owner, the runtime directory is moved aside without ever calling ``kill()``.
"""

from __future__ import annotations

import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .config import load_config, repo_root, sanitize_name, timestamp
from .runtime import runtime_context

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no fcntl
    fcntl = None

TERM_WAIT_SECONDS = 2.0
TERM_POLL_INTERVAL = 0.1
RECENT_SESSION_WINDOW_DAYS = 30


class LockUnavailableError(RuntimeError):
    """Raised when POSIX file locking isn't available on this platform."""


def _require_fcntl() -> None:
    if fcntl is None:
        raise LockUnavailableError(
            "Session locking requires a POSIX system (fcntl); not yet supported on this platform."
        )


# --- lock acquisition ---------------------------------------------------------------


@dataclass(frozen=True)
class LockHandle:
    path: Path
    fd: int
    pid: int


def acquire_lock(lock_path: Path) -> LockHandle | None:
    """Take the session lock non-blockingly. On success, writes this process's
    PID into the lock file so cancellation can later verify ownership instead
    of guessing from `ps` output. Returns None if the lock is already held."""
    _require_fcntl()
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    pid = os.getpid()
    os.ftruncate(fd, 0)
    os.write(fd, f"{pid}\n".encode("utf-8"))
    os.fsync(fd)
    return LockHandle(path=lock_path, fd=fd, pid=pid)


def release_lock(handle: LockHandle) -> None:
    _require_fcntl()
    try:
        fcntl.flock(handle.fd, fcntl.LOCK_UN)
    finally:
        os.close(handle.fd)


def read_lock_pid(lock_path: Path) -> int | None:
    if not lock_path.is_file():
        return None
    try:
        content = lock_path.read_text().strip()
    except OSError:
        return None
    return int(content) if content.isdigit() else None


def is_lock_held_by(lock_path: Path, pid: int) -> bool:
    """True if `pid` is plausibly the current exclusive holder of the lock,
    verified by attempting to take the lock ourselves rather than trusting the
    PID recorded in the file alone. If we *can* take it, nobody (including
    `pid`) currently holds it."""
    _require_fcntl()
    if not lock_path.is_file() or not pid_is_alive(pid):
        return False
    fd = os.open(str(lock_path), os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_cmdline(pid: int) -> str:
    proc_path = Path(f"/proc/{pid}/cmdline")
    if not proc_path.is_file():
        return ""
    try:
        raw = proc_path.read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()


def child_pids(pid: int) -> list[int]:
    """Direct children of `pid`, read from /proc/*/stat. Best-effort: returns
    an empty list on non-Linux systems or if /proc is unavailable."""
    children: list[int] = []
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return children
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            content = (entry / "stat").read_text()
        except OSError:
            continue
        close_paren = content.rfind(")")
        if close_paren == -1:
            continue
        stat_fields = content[close_paren + 2 :].split()
        if len(stat_fields) < 2:
            continue
        try:
            ppid = int(stat_fields[1])
        except ValueError:
            continue
        if ppid == pid:
            try:
                children.append(int(entry.name))
            except ValueError:
                continue
    return children


# --- conflict diagnosis --------------------------------------------------------------


def lock_path_for(runtime_dir: Path) -> Path:
    return runtime_dir / ".cdx-session.lock"


@dataclass(frozen=True)
class SessionDiagnosis:
    repo: Path
    runtime_dir: Path
    runtime_exists: bool
    lock_pid: int | None
    owner_alive: bool
    owner_verified: bool
    owner_cmdline: str


def diagnose_session(repo: Path, runtime_dir: Path) -> SessionDiagnosis:
    lock_path = lock_path_for(runtime_dir)
    pid = read_lock_pid(lock_path)
    alive = pid_is_alive(pid) if pid is not None else False
    verified = is_lock_held_by(lock_path, pid) if (pid is not None and alive) else False
    cmdline = read_cmdline(pid) if pid is not None else ""
    return SessionDiagnosis(
        repo=repo,
        runtime_dir=runtime_dir,
        runtime_exists=runtime_dir.is_dir(),
        lock_pid=pid,
        owner_alive=alive,
        owner_verified=verified,
        owner_cmdline=cmdline,
    )


@dataclass(frozen=True)
class LockAcquisition:
    handle: LockHandle | None
    diagnosis: SessionDiagnosis | None


def try_acquire(repo: Path, runtime_dir: Path) -> LockAcquisition:
    handle = acquire_lock(lock_path_for(runtime_dir))
    if handle is not None:
        return LockAcquisition(handle=handle, diagnosis=None)
    return LockAcquisition(handle=None, diagnosis=diagnose_session(repo, runtime_dir))


# --- cancellation --------------------------------------------------------------------


@dataclass(frozen=True)
class CancelResult:
    action: Literal["killed", "cleaned_up_only", "refused"]
    signaled_pids: tuple[int, ...] = ()
    moved_to: Path | None = None
    detail: str = ""


def _signal_quietly(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass


def _stale_path(runtime_dir: Path) -> Path:
    stale = runtime_dir.with_name(f"{runtime_dir.name}.stale.{timestamp()}")
    if stale.exists():
        stale = stale.with_name(f"{stale.name}.{os.getpid()}")
    return stale


def _move_aside(runtime_dir: Path) -> Path:
    stale = _stale_path(runtime_dir)
    runtime_dir.rename(stale)
    return stale


def cancel_session(repo: Path, runtime_dir: Path, dry_run: bool = False) -> CancelResult:
    """PID-verified cancellation. Never matches processes by argv/cwd
    substring: only ever signals the PID recorded in the lock file at
    acquisition time, and only after confirming that PID is (a) alive and (b)
    still the actual lock holder right now. A lock file naming a PID that is
    alive but no longer holds the lock is treated as unverifiable and refused,
    not signaled -- it may be stale, foreign, or have already exited and been
    reused by an unrelated process."""
    diagnosis = diagnose_session(repo, runtime_dir)

    if diagnosis.lock_pid is not None and diagnosis.owner_alive and not diagnosis.owner_verified:
        return CancelResult(
            "refused",
            detail=(
                f"lock file names PID {diagnosis.lock_pid} but it no longer holds the lock; "
                "refusing to signal a process we can't verify owns this session"
            ),
        )

    signaled: tuple[int, ...] = ()
    if diagnosis.lock_pid is not None and diagnosis.owner_alive and diagnosis.owner_verified:
        targets = [diagnosis.lock_pid, *child_pids(diagnosis.lock_pid)]
        if dry_run:
            return CancelResult(
                "killed", signaled_pids=tuple(targets), detail="dry-run: would TERM then KILL these PIDs"
            )
        for pid in targets:
            _signal_quietly(pid, signal.SIGTERM)
        deadline = time.monotonic() + TERM_WAIT_SECONDS
        while time.monotonic() < deadline and any(pid_is_alive(p) for p in targets):
            time.sleep(TERM_POLL_INTERVAL)
        for pid in targets:
            if pid_is_alive(pid):
                _signal_quietly(pid, signal.SIGKILL)
        signaled = tuple(targets)

    moved_to = None
    if runtime_dir.is_dir():
        if dry_run:
            moved_to = _stale_path(runtime_dir)
        else:
            moved_to = _move_aside(runtime_dir)

    action: Literal["killed", "cleaned_up_only"] = "killed" if signaled else "cleaned_up_only"
    return CancelResult(action, signaled_pids=signaled, moved_to=moved_to)


ConflictMode = Literal["diagnose", "cancel", "refuse"]


def handle_conflict(
    repo: Path, runtime_dir: Path, mode: ConflictMode = "diagnose", dry_run: bool = False
) -> tuple[SessionDiagnosis, CancelResult | None]:
    """Diagnose-only by default -- live cancellation must be explicitly
    requested via mode="cancel" by the caller (cli.py's interactive prompt or
    an explicit --cancel-active flag), never triggered implicitly."""
    diagnosis = diagnose_session(repo, runtime_dir)
    if mode != "cancel":
        return diagnosis, None
    return diagnosis, cancel_session(repo, runtime_dir, dry_run=dry_run)


# --- session log / transcript discovery -----------------------------------------------


def session_root_candidates(
    account_home: Path, runtime_dir: Path, legacy_runtime_dir: Path | None = None, codex_home_env: str | None = None
) -> list[Path]:
    candidates = [account_home / ".codex", runtime_dir]
    if legacy_runtime_dir is not None:
        candidates.append(legacy_runtime_dir)
    if codex_home_env:
        candidates.append(Path(codex_home_env))
    seen: list[Path] = []
    for candidate in candidates:
        if candidate not in seen:
            seen.append(candidate)
    return seen


def detected_session_dirs(roots: list[Path]) -> list[Path]:
    dirs = []
    for root in roots:
        for name in ("sessions", "archived_sessions"):
            candidate = root / name
            if candidate.is_dir():
                dirs.append(candidate)
    return dirs


def session_jsonl_files(roots: list[Path]) -> list[Path]:
    files: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        files.update(p for p in root.rglob("*.jsonl") if p.is_file())
    return sorted(files)


def recent_session_jsonl_count(roots: list[Path], within_days: int = RECENT_SESSION_WINDOW_DAYS) -> int:
    threshold = time.time() - within_days * 86400
    return sum(1 for f in session_jsonl_files(roots) if f.stat().st_mtime >= threshold)


# --- per-launch log directory ----------------------------------------------------------


def launch_log_dir(user_root: Path, repo_name: str) -> Path:
    return user_root / "codex_logs" / f"{timestamp()}_{sanitize_name(repo_name)}"


def raw_output_path(logdir: Path, stem: str = "command") -> Path:
    out_dir = logdir / "raw_outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{timestamp()}_{sanitize_name(stem.lower())}.txt"


def compressed_output_path(logdir: Path, stem: str = "command") -> Path:
    out_dir = logdir / "compressed_outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{timestamp()}_{sanitize_name(stem.lower())}.txt"


# --- CLI commands --------------------------------------------------------------------


def command_session_doctor(args) -> int:
    cfg = load_config(getattr(args, "config", None))
    repo = repo_root(Path(args.repo))
    access_mode = "full" if args.full else "safe"
    rctx = runtime_context(cfg, repo, access_mode=access_mode, engine=args.engine)
    diagnosis = diagnose_session(repo, rctx.runtime_dir)
    print(f"repo={diagnosis.repo}")
    print(f"runtime_dir={diagnosis.runtime_dir}")
    print(f"runtime_exists={diagnosis.runtime_exists}")
    print(f"lock_pid={diagnosis.lock_pid}")
    print(f"owner_alive={diagnosis.owner_alive}")
    print(f"owner_verified={diagnosis.owner_verified}")
    if diagnosis.owner_cmdline:
        print(f"owner_cmdline={diagnosis.owner_cmdline}")
    return 0


def command_cancel_active(args) -> int:
    cfg = load_config(getattr(args, "config", None))
    repo = repo_root(Path(args.repo))
    access_mode = "full" if args.full else "safe"
    rctx = runtime_context(cfg, repo, access_mode=access_mode, engine=args.engine)
    result = cancel_session(repo, rctx.runtime_dir, dry_run=args.dry_run)
    print(f"action={result.action}")
    if result.signaled_pids:
        print(f"signaled_pids={list(result.signaled_pids)}")
    if result.moved_to:
        print(f"moved_to={result.moved_to}")
    if result.detail:
        print(result.detail)
    return 1 if result.action == "refused" else 0


__all__ = [
    "CancelResult",
    "ConflictMode",
    "LockAcquisition",
    "LockHandle",
    "LockUnavailableError",
    "SessionDiagnosis",
    "acquire_lock",
    "cancel_session",
    "child_pids",
    "command_cancel_active",
    "command_session_doctor",
    "compressed_output_path",
    "detected_session_dirs",
    "diagnose_session",
    "handle_conflict",
    "is_lock_held_by",
    "launch_log_dir",
    "lock_path_for",
    "pid_is_alive",
    "raw_output_path",
    "read_cmdline",
    "read_lock_pid",
    "recent_session_jsonl_count",
    "release_lock",
    "session_jsonl_files",
    "session_root_candidates",
    "try_acquire",
]
