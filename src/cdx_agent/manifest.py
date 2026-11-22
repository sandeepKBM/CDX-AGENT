"""Run manifests: <run_dir>/MANIFEST.json (schema_version 1) plus one index line per run in <repo>/.cdx/jobs.jsonl.

    write(run_dir, **keys)   atomic create (tmp + os.replace); keys not given are null; appends the index line
    update(run_dir, **keys)  atomic rewrite with changed keys (finished, exit_code, state, ...)
    read(run_dir)            the manifest dict

``run_dir`` is normally <repo>/.cdx/runs/<job_id>, which also fixes the index file (<repo>/.cdx/jobs.jsonl);
pass ``index=PATH`` to write() to put it elsewhere (``index=None`` -> derived, ``index=""`` -> no index line).
"""
from __future__ import annotations

import json
import os
from typing import Any

SCHEMA_VERSION = 1
FILENAME = "MANIFEST.json"
INDEX_KEYS = ("job_id", "name", "project", "command", "started", "state")
KEYS = (
    # identity
    "schema_version", "job_id", "name", "project", "session_id", "agent_id",
    # command
    "command", "cwd", "entry", "overrides", "config_hash",
    # code: {repo, head, dirty, diff_sha256, snapshot}
    "code",
    # environment and placement: placement = {class, slurm_job, step, node, gpus, locks}
    "env", "host", "placement", "resources", "footprint",
    # science
    "world_set_hash", "seeds", "assumptions", "guarantee_type", "proposer",
    # outcome: outputs = [{path, bytes, tier}]
    "started", "finished", "exit_code", "state", "outputs", "scratch_dirs", "log",
)


def _atomic_json(path: str, data: dict[str, Any]) -> None:
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=False, default=str)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def default_index(run_dir: str) -> str:
    """<repo>/.cdx/jobs.jsonl for a run dir laid out as <repo>/.cdx/runs/<job_id>."""
    runs = os.path.dirname(os.path.abspath(run_dir.rstrip("/")))
    cdx = os.path.dirname(runs)
    return os.path.join(cdx, "jobs.jsonl")


def read(run_dir: str) -> dict[str, Any]:
    with open(os.path.join(run_dir, FILENAME), encoding="utf-8") as fh:
        return json.load(fh)


def write(run_dir: str, index: str | None = None, **keys: Any) -> dict[str, Any]:
    """Creates MANIFEST.json (all spec keys, missing ones null) and appends one index line."""
    os.makedirs(run_dir, exist_ok=True)
    data: dict[str, Any] = {k: None for k in KEYS}
    data.update(keys)
    data["schema_version"] = SCHEMA_VERSION
    _atomic_json(os.path.join(run_dir, FILENAME), data)
    if index != "":
        path = index or default_index(run_dir)
        line = {k: data.get(k) for k in INDEX_KEYS}
        line["run_dir"] = os.path.abspath(run_dir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, (json.dumps(line, default=str) + "\n").encode("utf-8"))  # one write: lines never interleave
        finally:
            os.close(fd)
    return data


def update(run_dir: str, **keys: Any) -> dict[str, Any]:
    """Rewrites MANIFEST.json with ``keys`` replaced (shallow); creates it from nulls when absent."""
    try:
        data = read(run_dir)
    except (OSError, ValueError):
        data = {k: None for k in KEYS}
    data.update(keys)
    data["schema_version"] = SCHEMA_VERSION
    os.makedirs(run_dir, exist_ok=True)
    _atomic_json(os.path.join(run_dir, FILENAME), data)
    return data
