"""Append-only delete audit log: one JSON line per decision in <dir>/delete_<shorthost>.jsonl.

dir = $CDX_AUDIT_DIR, else /common/users/ss5772/.cdx/audit. Writes use flock + fsync; log() never raises.
"""
from __future__ import annotations

import fcntl
import json
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Dict

DEFAULT_DIR = "/common/users/ss5772/.cdx/audit"
ACTIONS = ("deleted", "trashed", "denied", "restored", "purged", "drained", "allowed")


def short_host() -> str:
    return socket.gethostname().split(".")[0]


def audit_dir() -> Path:
    return Path(os.environ.get("CDX_AUDIT_DIR") or DEFAULT_DIR)


def audit_file() -> Path:
    return audit_dir() / f"delete_{short_host()}.jsonl"


def append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    """Locked, fsynced single-line append. Raises OSError."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, sort_keys=True, default=str) + "\n"
    with open(path, "a", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def project_name() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return os.path.basename(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else ""


def log(action: str, path: Any, **fields: Any) -> bool:
    """Append one audit record. Returns False (never raises) when the write fails."""
    try:
        rec: Dict[str, Any] = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "host": short_host(),
            "session": os.environ.get("CLAUDE_SESSION_ID") or os.environ.get("CDX_SESSION_ID") or "",
            "agent": os.environ.get("CDX_AGENT_ID", ""),
            "project": project_name(),
            "action": action,
            "path": str(path),
        }
        rec.update(fields)
        append_jsonl(audit_file(), rec)
        return True
    except (OSError, ValueError, TypeError):
        return False
