"""Trash with manifest, restore, TTL purge and staging drain (WP3).

    python -m cdx_agent.trash rm PATH... [--dry-run] [--from report.json] [--verified-copy]
    python -m cdx_agent.trash list [--json] | restore ID | purge [--include-critical] [--now ISO]
    python -m cdx_agent.trash drain [--dest D] | status

Moves into trash are os.rename only, so a trash root must be on the same filesystem as the path.
Layout: <root>/<YYYY-MM-DD>/<id>/{payload/<basename>, entry.json, MANIFEST.sha256}.
Index: JSONL events (trashed/restored/purged/drained) so every host can list; state = fold of events.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import secrets
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from cdx_agent import audit

DEFAULT_ROOTS = [
    "/data/local/ss5772/trash",
    "/freespace/local/ss5772/trash",
    "/common/users/ss5772/.cdx/trash_staging",
    "/common/home/ss5772/.cdx/trash_staging",
]
DEFAULT_INDEX = "/common/users/ss5772/.cdx/trash/index.jsonl"
DEFAULT_DEST = "/data/local/ss5772/trash"
NO_ROOT_MSG = "no trash root on this filesystem; add one to CDX_TRASH_ROOTS"
EXIT_OK, EXIT_REFUSED = 0, 2


# ---------------------------------------------------------------- config

def configured_roots() -> List[str]:
    raw = os.environ.get("CDX_TRASH_ROOTS")
    if raw:
        try:
            val = json.loads(raw)
            if isinstance(val, list):
                return [str(v) for v in val]
        except ValueError:
            pass
    return list(DEFAULT_ROOTS)


def index_path() -> Path:
    return Path(os.environ.get("CDX_TRASH_INDEX") or DEFAULT_INDEX)


def is_staging(root: str) -> bool:
    return os.path.basename(root.rstrip("/")).endswith("_staging")


def _value(key: str, default: Any) -> Any:
    try:
        mod = importlib.import_module("cdx_agent.values")
        return mod.get(key, default=default)
    except Exception:
        return default


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(text: str) -> datetime:
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _dev_of_root(root: str) -> Optional[int]:
    """st_dev of the root, or of its parent when the root is creatable; None when unusable."""
    for cand in (root, os.path.dirname(root.rstrip("/")) or "/"):
        try:
            return os.stat(cand).st_dev
        except OSError:
            continue
    return None


def pick_root(path: str, create: bool = True) -> Optional[str]:
    try:
        dev = os.lstat(path).st_dev
    except OSError:
        return None
    for root in configured_roots():
        if _dev_of_root(root) != dev:
            continue
        if not os.path.isdir(root):
            if not create:
                return root
            try:
                os.mkdir(root, 0o700)
            except OSError:
                continue
        return root
    return None


def _inside(path: str, root: str) -> bool:
    rp, rr = os.path.realpath(path), os.path.realpath(root)
    return rp != rr and rp.startswith(rr.rstrip("/") + "/")


def _inside_any_root(path: str) -> bool:
    return any(_inside(path, r) for r in configured_roots())


# ---------------------------------------------------------------- hashing / sizes

def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tree_size(path: str) -> Tuple[int, int]:
    """(bytes, files): regular-file st_size sum and count of non-directory entries; symlinks not followed."""
    if os.path.islink(path) or not os.path.isdir(path):
        st = os.lstat(path)
        return (0 if os.path.islink(path) else st.st_size), 1
    nbytes = files = 0
    for dp, dn, fn in os.walk(path):
        for name in dn:
            if os.path.islink(os.path.join(dp, name)):
                files += 1
        for name in fn:
            files += 1
            p = os.path.join(dp, name)
            if not os.path.islink(p):
                nbytes += os.lstat(p).st_size
    return nbytes, files


def manifest_lines(payload: str) -> List[str]:
    out = []
    for dp, _dn, fn in os.walk(payload):
        for name in fn:
            p = os.path.join(dp, name)
            if os.path.islink(p):
                continue
            out.append(f"{_sha256(p)}  {os.path.relpath(p, payload)}")
    return sorted(out, key=lambda ln: ln.split("  ", 1)[1])


def verify_manifest(entry_dir: str) -> List[str]:
    """Return mismatch descriptions (empty = payload matches MANIFEST.sha256)."""
    payload = os.path.join(entry_dir, "payload")
    try:
        want = Path(entry_dir, "MANIFEST.sha256").read_text(encoding="utf-8").split("\n")
    except OSError as exc:
        return [f"manifest unreadable: {exc}"]
    want = sorted(ln for ln in want if ln)
    have = sorted(manifest_lines(payload))
    return sorted(set(want) ^ set(have)) if want != have else []


# ---------------------------------------------------------------- index

def _index_event(event: str, **fields: Any) -> None:
    audit.append_jsonl(index_path(), {"event": event, "time": _utcnow().isoformat(), **fields})


def index_state() -> Dict[str, Dict[str, Any]]:
    state: Dict[str, Dict[str, Any]] = {}
    try:
        lines = index_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return state
    for ln in lines:
        try:
            ev = json.loads(ln)
        except ValueError:
            continue
        kind, eid = ev.get("event"), ev.get("id")
        if not eid:
            continue
        if kind == "trashed":
            state[eid] = {k: v for k, v in ev.items() if k not in ("event", "time")}
        elif kind in ("restored", "purged"):
            state.pop(eid, None)
        elif kind == "drained" and eid in state:
            state[eid].update({k: v for k, v in ev.items() if k in ("entry_dir", "filesystem")})
    return state


def _staging_bytes(root: str) -> int:
    return sum(int(e.get("bytes", 0)) for e in index_state().values()
               if e.get("filesystem") == root and os.path.isdir(e.get("entry_dir", "")))


# ---------------------------------------------------------------- rm

def _classify(path: str, verified_copy: bool) -> Any:
    dp = importlib.import_module("cdx_agent.delete_policy")
    return dp.classify(Path(path), dp.default_ctx(verified_copy=verified_copy))


def _in_use(path: str) -> Dict[str, Any]:
    return importlib.import_module("cdx_agent.inuse").in_use(Path(path))


def _rm_tree(path: str) -> None:
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    else:
        os.unlink(path)


def _session() -> str:
    return os.environ.get("CLAUDE_SESSION_ID") or os.environ.get("CDX_SESSION_ID") or ""


def rm(paths: Sequence[str], dry_run: bool = False, verified_copy: bool = False,
       now: Optional[datetime] = None) -> int:
    now = now or _utcnow()
    command = " ".join(sys.argv)
    code = EXIT_OK
    for raw in paths:
        path = os.path.abspath(raw)
        if not os.path.lexists(path):
            print(f"{raw}: not found")
            code = EXIT_REFUSED
            continue
        nbytes, nfiles = tree_size(path)
        dec = _classify(path, verified_copy)
        common = dict(tier=dec.tier, rule_id=dec.rule_id, bytes=nbytes, command=command)
        if dec.tier == "c":
            print(f"protected ({dec.rule_id}): only the user may delete this: {path}")
            if not dry_run:
                audit.log("denied", path, **common)
            code = EXIT_REFUSED
            continue
        use = _in_use(path)
        verdict = use.get("verdict")
        if verdict != "free":
            print(f"refused: {path} is in use ({verdict}): {use.get('evidence')}")
            if not dry_run:
                audit.log("denied", path, in_use=use, **common)
            code = EXIT_REFUSED
            continue
        if _inside_any_root(path):
            print(f"refused: {path} is inside a trash root")
            code = EXIT_REFUSED
            continue
        if dec.tier == "a":
            if dry_run:
                print(f"{path}: tier=a rule={dec.rule_id} action=delete")
                continue
            _rm_tree(path)
            audit.log("deleted", path, in_use=use, **common)
            print(f"deleted {path}")
            continue
        root = pick_root(path, create=not dry_run)
        if root is None:
            print(f"{path}: {NO_ROOT_MSG}")
            if not dry_run:
                audit.log("denied", path, in_use=use, **common)
            code = EXIT_REFUSED
            continue
        if is_staging(root):
            cap = float(_value("trash.nfs_staging_cap_gb", 10)) * 1024 ** 3
            if _staging_bytes(root) + nbytes > cap:
                print(f"refused: NFS staging cap exceeded ({root}); run `trash drain` on westeros or purge")
                if not dry_run:
                    audit.log("denied", path, in_use=use, **common)
                code = EXIT_REFUSED
                continue
        if dry_run:
            print(f"{path}: tier=b rule={dec.rule_id} action=trash root={root}")
            continue
        ttl = float(_value("trash.ttl_days", 7))
        eid = f"{now.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
        entry_dir = os.path.join(root, now.strftime("%Y-%m-%d"), eid)
        payload = os.path.join(entry_dir, "payload")
        os.makedirs(payload, mode=0o700)
        try:
            os.rename(path, os.path.join(payload, os.path.basename(path)))
        except OSError as exc:
            shutil.rmtree(entry_dir, ignore_errors=True)
            print(f"refused: rename failed for {path}: {exc}")
            audit.log("denied", path, in_use=use, **common)
            code = EXIT_REFUSED
            continue
        entry = {
            "id": eid, "original_path": path, "host": audit.short_host(), "filesystem": root,
            "bytes": nbytes, "files": nfiles, "deleted_at": now.isoformat(),
            "expires_at": (now + timedelta(days=ttl)).isoformat(),
            "session_id": _session(), "agent_id": os.environ.get("CDX_AGENT_ID", ""),
            "project": audit.project_name(), "tier": dec.tier, "rule_id": dec.rule_id,
            "critical": bool(getattr(dec, "critical", False)), "in_use_result": use, "command": command,
        }
        Path(entry_dir, "entry.json").write_text(json.dumps(entry, indent=2, sort_keys=True), encoding="utf-8")
        Path(entry_dir, "MANIFEST.sha256").write_text("".join(ln + "\n" for ln in manifest_lines(payload)),
                                                      encoding="utf-8")
        _index_event("trashed", entry_dir=entry_dir, **entry)
        audit.log("trashed", path, trash_id=eid, in_use=use, **common)
        print(f"trashed {path} -> {eid}")
    return code


def from_report(json_file: str, dry_run: bool = False, verified_copy: bool = False) -> int:
    rows = json.loads(Path(json_file).read_text(encoding="utf-8"))
    paths = [r["path"] for r in rows if isinstance(r, dict) and r.get("path")]
    return rm(paths, dry_run=dry_run, verified_copy=verified_copy)


# ---------------------------------------------------------------- restore

def _copy_tree(src: str, dst: str) -> None:
    if os.path.isdir(src) and not os.path.islink(src):
        shutil.copytree(src, dst, symlinks=True)
    else:
        shutil.copy2(src, dst, follow_symlinks=False)


def restore(eid: str) -> int:
    entry = index_state().get(eid)
    if not entry:
        print(f"unknown trash id: {eid}")
        return EXIT_REFUSED
    entry_dir = entry["entry_dir"]
    if not os.path.isdir(entry_dir):
        print(f"{eid}: entry is on host {entry.get('host')} ({entry_dir}); run restore there")
        return EXIT_REFUSED
    orig = entry["original_path"]
    if os.path.lexists(orig):
        print(f"refused: original path exists: {orig}")
        return EXIT_REFUSED
    bad = verify_manifest(entry_dir)
    if bad:
        print(f"refused: manifest mismatch for {eid}: {bad[:5]}")
        return EXIT_REFUSED
    src = os.path.join(entry_dir, "payload", os.path.basename(orig))
    os.makedirs(os.path.dirname(orig), exist_ok=True)
    try:
        os.rename(src, orig)
    except OSError:
        # different filesystem (e.g. drained entry): copy beside the target, rename into place
        tmp = f"{orig}.restoring-{eid}"
        try:
            _copy_tree(src, tmp)
            os.rename(tmp, orig)
        except OSError as exc:
            print(f"refused: cannot restore to {orig}: {exc}")
            return EXIT_REFUSED
    shutil.rmtree(entry_dir, ignore_errors=True)
    _index_event("restored", id=eid, original_path=orig)
    audit.log("restored", orig, trash_id=eid, tier=entry.get("tier"), rule_id=entry.get("rule_id"),
              bytes=entry.get("bytes"), command=" ".join(sys.argv))
    print(f"restored {eid} -> {orig}")
    return EXIT_OK


# ---------------------------------------------------------------- purge / drain / status

def purge(now: Optional[datetime] = None, include_critical: bool = False) -> List[str]:
    now = now or _utcnow()
    done: List[str] = []
    for eid, e in sorted(index_state().items()):
        try:
            if _parse_ts(e["expires_at"]) >= now:
                continue
        except (KeyError, ValueError):
            continue
        if e.get("critical") and not include_critical:
            continue
        entry_dir = e.get("entry_dir", "")
        if not os.path.isdir(entry_dir) or not _inside_any_root(entry_dir):
            continue
        shutil.rmtree(entry_dir, ignore_errors=True)
        try:
            os.rmdir(os.path.dirname(entry_dir))
        except OSError:
            pass
        _index_event("purged", id=eid)
        audit.log("purged", e.get("original_path", ""), trash_id=eid, tier=e.get("tier"),
                  rule_id=e.get("rule_id"), bytes=e.get("bytes"), command=" ".join(sys.argv))
        done.append(eid)
    return done


def drain(dest_root: str = DEFAULT_DEST) -> Dict[str, List[str]]:
    res: Dict[str, List[str]] = {"drained": [], "failed": []}
    if not os.path.isdir(dest_root):
        print(f"drain: destination {dest_root} not present on this host")
        return res
    staging = [r for r in configured_roots() if is_staging(r)]
    for eid, e in sorted(index_state().items()):
        entry_dir = e.get("entry_dir", "")
        if e.get("filesystem") not in staging or not os.path.isdir(entry_dir):
            continue
        new_dir = os.path.join(dest_root, os.path.basename(os.path.dirname(entry_dir)), eid)
        try:
            if os.path.exists(new_dir):
                shutil.rmtree(new_dir)
            os.makedirs(os.path.dirname(new_dir), exist_ok=True)
            shutil.copytree(entry_dir, new_dir, symlinks=True)
            bad = verify_manifest(new_dir)
        except OSError as exc:
            bad = [str(exc)]
        if bad:
            shutil.rmtree(new_dir, ignore_errors=True)
            print(f"drain: verify failed for {eid}, staging copy kept: {bad[:5]}")
            res["failed"].append(eid)
            continue
        meta_path = Path(new_dir, "entry.json")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["filesystem"] = dest_root
        meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
        shutil.rmtree(entry_dir)
        try:
            os.rmdir(os.path.dirname(entry_dir))
        except OSError:
            pass
        _index_event("drained", id=eid, entry_dir=new_dir, filesystem=dest_root)
        audit.log("drained", e.get("original_path", ""), trash_id=eid, tier=e.get("tier"),
                  rule_id=e.get("rule_id"), bytes=e.get("bytes"), command=" ".join(sys.argv))
        res["drained"].append(eid)
    return res


def status() -> Dict[str, Any]:
    roots: Dict[str, Dict[str, Any]] = {}
    critical = 0
    for e in index_state().values():
        r = roots.setdefault(e.get("filesystem", "?"),
                             {"entries": 0, "bytes": 0, "oldest": None, "next_expiry": None})
        r["entries"] += 1
        r["bytes"] += int(e.get("bytes", 0))
        da, ex = e.get("deleted_at"), e.get("expires_at")
        if da and (r["oldest"] is None or da < r["oldest"]):
            r["oldest"] = da
        if ex and (r["next_expiry"] is None or ex < r["next_expiry"]):
            r["next_expiry"] = ex
        critical += 1 if e.get("critical") else 0
    return {"roots": roots, "critical": critical}


# ---------------------------------------------------------------- CLI

def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="cdx_agent.trash")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("rm")
    p.add_argument("paths", nargs="*")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--from", dest="report")
    p.add_argument("--verified-copy", action="store_true")
    p = sub.add_parser("list")
    p.add_argument("--json", action="store_true")
    sub.add_parser("restore").add_argument("id")
    p = sub.add_parser("purge")
    p.add_argument("--include-critical", action="store_true")
    p.add_argument("--now")
    sub.add_parser("drain").add_argument("--dest", default=DEFAULT_DEST)
    sub.add_parser("status")
    a = ap.parse_args(argv)
    if a.cmd == "rm":
        if not a.paths and not a.report:
            ap.error("rm needs PATH... or --from report.json")
        code = rm(a.paths, dry_run=a.dry_run, verified_copy=a.verified_copy) if a.paths else EXIT_OK
        if a.report:
            code = max(code, from_report(a.report, dry_run=a.dry_run, verified_copy=a.verified_copy))
        return code
    if a.cmd == "list":
        ents = list(index_state().values())
        if a.json:
            print(json.dumps(ents, indent=2, sort_keys=True))
        else:
            for e in ents:
                print(f"{e['id']}  {e.get('bytes', 0):>12}  exp {e.get('expires_at', '')[:10]}  {e['original_path']}")
        return EXIT_OK
    if a.cmd == "restore":
        return restore(a.id)
    if a.cmd == "purge":
        for eid in purge(now=_parse_ts(a.now) if a.now else None, include_critical=a.include_critical):
            print(f"purged {eid}")
        return EXIT_OK
    if a.cmd == "drain":
        return EXIT_REFUSED if drain(a.dest)["failed"] else EXIT_OK
    print(json.dumps(status(), indent=2, sort_keys=True))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
