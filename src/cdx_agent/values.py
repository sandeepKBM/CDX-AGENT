"""Machine-written ledger of tunable / ASSUMED numbers.

Code reads the ledger; agents change values through the CLI with a reason. Never hard-code these numbers.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import socket
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_VALUES_FILE = "/common/users/ss5772/.cdx/values.jsonl"
DEFAULT_SEED = Path(__file__).resolve().parents[2] / "tools/templates/values.seed.jsonl"
TAGS = ("MEASURED", "MODEL", "ASSUMED", "USER")
KEY_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")
_MISSING: Any = object()


def values_path(path: str | os.PathLike[str] | None = None) -> Path:
    if path is not None:
        return Path(path)
    return Path(os.environ.get("CDX_VALUES_FILE") or DEFAULT_VALUES_FILE)


def _read_rows(path: str | os.PathLike[str] | None) -> list[dict[str, Any]]:
    try:
        text = values_path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("key"), str) and "value" in obj:
            rows.append(obj)
    return rows


def current(path: str | os.PathLike[str] | None = None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for r in _read_rows(path):
        out[r["key"]] = r
    return out


def row(key: str, path: str | os.PathLike[str] | None = None) -> dict[str, Any] | None:
    return current(path).get(key)


def get(key: str, default: Any = _MISSING, path: str | os.PathLike[str] | None = None) -> Any:
    r = row(key, path)
    if r is None:
        if default is _MISSING:
            raise KeyError(key)
        return default
    return r["value"]


def history(key: str, path: str | os.PathLike[str] | None = None) -> list[dict[str, Any]]:
    return [r for r in _read_rows(path) if r["key"] == key]


def _validate(key: str, tag: str, reason: str | None, source: str | None) -> None:
    if not isinstance(key, str) or not KEY_RE.match(key):
        raise ValueError(f"invalid key {key!r}: must match {KEY_RE.pattern}")
    if tag not in TAGS:
        raise ValueError(f"invalid tag {tag!r}: must be one of {'|'.join(TAGS)}")
    if not reason or not str(reason).strip():
        raise ValueError("reason is required")
    if tag == "MEASURED" and not (source and str(source).strip()):
        raise ValueError("MEASURED values require a source")


def _append(r: dict[str, Any], path: str | os.PathLike[str] | None) -> None:
    p = values_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(r) + "\n"
    with open(p, "a", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def set_value(
    key: str,
    value: Any,
    *,
    tag: str,
    reason: str,
    unit: str | None = None,
    source: str | None = None,
    by: str | None = None,
    path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    _validate(key, tag, reason, source)
    r = {
        "key": key,
        "value": value,
        "unit": unit,
        "tag": tag,
        "reason": reason,
        "source": source,
        "by": by or os.environ.get("CDX_AGENT_ID") or os.environ.get("USER") or "unknown",
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "host": socket.gethostname().split(".")[0],
    }
    _append(r, path)
    return r


def seed(seed_file: str | os.PathLike[str], path: str | os.PathLike[str] | None = None) -> list[str]:
    have = set(current(path))
    added: list[str] = []
    for line in Path(seed_file).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        s = json.loads(line)
        key = s["key"]
        if key in have:
            continue
        set_value(
            key,
            s["value"],
            tag=s["tag"],
            reason=s["reason"],
            unit=s.get("unit"),
            source=s.get("source"),
            by=s.get("by"),
            path=path,
        )
        have.add(key)
        added.append(key)
    return added


def _fmt(r: dict[str, Any]) -> str:
    unit = r.get("unit") or "-"
    return f"{r['key']}={json.dumps(r['value'])} {unit} {r.get('tag')} {r.get('at', '-')} {r.get('reason')}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m cdx_agent.values", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("get")
    g.add_argument("key")
    g.add_argument("--json", action="store_true")
    s = sub.add_parser("set")
    s.add_argument("key")
    s.add_argument("value")
    s.add_argument("--tag", required=True)
    s.add_argument("--reason", required=True)
    s.add_argument("--unit")
    s.add_argument("--source")
    s.add_argument("--by")
    ls = sub.add_parser("list")
    ls.add_argument("--tag")
    ls.add_argument("--json", action="store_true")
    h = sub.add_parser("history")
    h.add_argument("key")
    sd = sub.add_parser("seed")
    sd.add_argument("--file", default=str(DEFAULT_SEED))
    a = ap.parse_args(argv)

    if a.cmd == "get":
        r = row(a.key)
        if r is None:
            print(f"missing key: {a.key}", file=sys.stderr)
            return 1
        if a.json:
            print(json.dumps(r))
        else:
            print(r["value"] if isinstance(r["value"], str) else json.dumps(r["value"]))
        return 0
    if a.cmd == "set":
        try:
            value = json.loads(a.value)
        except ValueError:
            value = a.value
        try:
            r = set_value(a.key, value, tag=a.tag, reason=a.reason, unit=a.unit, source=a.source, by=a.by)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(_fmt(r))
        return 0
    if a.cmd == "list":
        rows = sorted((r for r in current().values() if not a.tag or r.get("tag") == a.tag), key=lambda r: r["key"])
        if a.json:
            print(json.dumps(rows))
        else:
            for r in rows:
                print(_fmt(r))
        return 0
    if a.cmd == "history":
        rows = history(a.key)
        if not rows:
            print(f"missing key: {a.key}", file=sys.stderr)
            return 1
        for r in rows:
            print(_fmt(r))
        return 0
    try:
        added = seed(a.file)
    except (OSError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print("added: " + (", ".join(added) if added else "(none)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
