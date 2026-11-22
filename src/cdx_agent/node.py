"""Node-local storage setup/status/release (WP7). Runs ON a node; later wrapped as a --run step.

    python -m cdx_agent.node setup|status|release --spec .cdx/node_env.yaml [--root DIR] [--dry-run]

Spec (YAML):
    project: NAME                      # root defaults to /freespace/local/$USER/NAME
    items:   [{name, source, dest, sha256?}]   source = coldbank:<name> or an NFS path; dest relative to root
    env:     {VAR: value}              # extra exports for env.sh
    outputs: [{name, path, dest, keep: true, sha256?}]   path relative to root, dest = NFS path

A tree digest is the sha256 of the sorted "<sha256>  <relpath>" manifest lines of the tree (of the bytes for a file).
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import yaml

from cdx_agent import audit, trash

CACHE_VARS = {
    "PIP_CACHE_DIR": "pip", "UV_CACHE_DIR": "uv", "CONDA_PKGS_DIRS": "conda_pkgs", "HF_HOME": "hf",
    "TORCHINDUCTOR_CACHE_DIR": "torchinductor", "TRITON_CACHE_DIR": "triton", "NUMBA_CACHE_DIR": "numba",
    "XDG_CACHE_HOME": "xdg",
}
EXIT_OK, EXIT_FAIL = 0, 1


class NodeError(Exception):
    pass


def load_spec(path: str) -> Dict[str, Any]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise NodeError(f"{path}: spec must be a mapping")
    data.setdefault("items", [])
    data.setdefault("outputs", [])
    data.setdefault("env", {})
    for it in data["items"]:
        for k in ("name", "source", "dest"):
            if not it.get(k):
                raise NodeError(f"item missing {k}: {it}")
    for o in data["outputs"]:
        for k in ("name", "path", "dest"):
            if not o.get(k):
                raise NodeError(f"output missing {k}: {o}")
    return data


def default_root(spec: Dict[str, Any]) -> str:
    project = spec.get("project") or os.path.basename(os.getcwd())
    return f"/freespace/local/{os.environ.get('USER', 'user')}/{project}"


def tree_digest(path: str) -> str:
    if os.path.isfile(path):
        return trash._sha256(path)
    text = "".join(ln + "\n" for ln in trash.manifest_lines(path))
    return hashlib.sha256(text.encode()).hexdigest()


def _under(root: str, rel: str) -> str:
    p = os.path.normpath(os.path.join(root, rel))
    if p != root and not p.startswith(root.rstrip("/") + "/"):
        raise NodeError(f"{rel}: path escapes root {root}")
    return p


def _copy(src: str, dst: str, link: bool = False) -> None:
    if os.path.isdir(src):
        def cp(s: str, d: str, *, follow_symlinks: bool = True) -> str:
            if link:
                try:
                    os.link(s, d)
                    return d
                except OSError:
                    pass
            return shutil.copy2(s, d, follow_symlinks=follow_symlinks)

        shutil.copytree(src, dst, symlinks=True, copy_function=cp)
    else:
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        shutil.copy2(src, dst)


def coldbank_fetch(name: str, cache: str) -> str:
    """Fetch through coldbank's own API into a node-local cache; returns the cache entry path."""
    from cdx_agent import coldbank

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = coldbank.main(["fetch", name, "--cache", cache, "--no-lease"])
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    if code != 0 or not lines:
        raise NodeError(f"coldbank fetch {name} failed (exit {code})")
    return lines[-1].strip()


def _entry(dest: str) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(Path(dest, ".entry.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _reusable(item: Dict[str, Any], dest: str) -> bool:
    e = _entry(dest)
    if not e or not os.path.isdir(dest):
        return False
    want = item.get("sha256")
    return e.get("tree_sha256") == want if want else e.get("source") == item["source"]


def plan_item(item: Dict[str, Any], root: str) -> str:
    dest = _under(root, item["dest"])
    if _reusable(item, dest):
        return f"reuse   {item['name']}: {dest}"
    return f"fetch   {item['name']}: {item['source']} -> {dest}.partial -> {dest}"


def env_text(spec: Dict[str, Any], root: str) -> str:
    lines = ["# written by cdx_agent.node setup"]
    for var, sub in CACHE_VARS.items():
        lines.append(f"export {var}={shlex.quote(os.path.join(root, 'cache', sub))}")
    lines.append(f"export TMPDIR={shlex.quote(os.path.join(root, 'tmp'))}")
    lines.append(f"export CDX_NODE_ROOT={shlex.quote(root)}")
    for k, v in sorted((spec.get("env") or {}).items()):
        lines.append(f"export {k}={shlex.quote(str(v).replace('$ROOT', root))}")
    return "\n".join(lines) + "\n"


def setup(spec: Dict[str, Any], root: str, dry_run: bool = False,
          fetch: Optional[Any] = None) -> int:
    fetch = fetch or coldbank_fetch
    if dry_run:
        print(f"root {root}")
        for it in spec["items"]:
            print(plan_item(it, root))
        print(f"write   {os.path.join(root, 'env.sh')}")
        return EXIT_OK
    os.makedirs(root, exist_ok=True)
    for sub in list(CACHE_VARS.values()):
        os.makedirs(os.path.join(root, "cache", sub), exist_ok=True)
    os.makedirs(os.path.join(root, "tmp"), exist_ok=True)
    code = EXIT_OK
    for it in spec["items"]:
        dest = _under(root, it["dest"])
        if _reusable(it, dest):
            print(f"reuse {it['name']}")
            continue
        partial = dest + ".partial"
        try:
            if os.path.lexists(partial):
                raise NodeError(f"{partial} exists (interrupted run?); trash it with cdx_agent.trash rm")
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            src = it["source"]
            if src.startswith("coldbank:"):
                path = fetch(src.split(":", 1)[1], os.path.join(root, "cache", "coldbank"))
                _copy(path, partial, link=True)
            else:
                _copy(src, partial)
            digest = tree_digest(partial)
            if it.get("sha256") and digest != it["sha256"]:
                raise NodeError(f"{it['name']}: sha256 mismatch (got {digest}, want {it['sha256']})")
            if not src.startswith("coldbank:") and os.path.exists(src) and tree_digest(src) != digest:
                raise NodeError(f"{it['name']}: copy does not match source")
            if os.path.lexists(dest):
                raise NodeError(f"{dest} exists with a stale entry; trash it with cdx_agent.trash rm")
            os.rename(partial, dest)
            if os.path.isdir(dest):
                Path(dest, ".entry.json").write_text(json.dumps(
                    {"name": it["name"], "source": src, "tree_sha256": digest,
                     "time": datetime.now(timezone.utc).isoformat()}, indent=2), encoding="utf-8")
            print(f"fetched {it['name']} -> {dest}")
        except (NodeError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            code = EXIT_FAIL
    tmp = os.path.join(root, "env.sh.tmp")
    Path(tmp).write_text(env_text(spec, root), encoding="utf-8")
    os.replace(tmp, os.path.join(root, "env.sh"))
    print(f"wrote {os.path.join(root, 'env.sh')}")
    return code


def _quota_lines() -> List[str]:
    exe = shutil.which("quota")
    if not exe:
        return []
    try:
        r = subprocess.run([exe, "-s"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def status(spec: Dict[str, Any], root: str) -> int:
    code = EXIT_OK
    for it in spec["items"]:
        dest = _under(root, it["dest"])
        if not os.path.exists(dest):
            print(f"{it['name']}: missing")
            code = EXIT_FAIL
            continue
        nbytes, _ = trash.tree_size(dest)
        e = _entry(dest)
        ok = bool(e) and e.get("tree_sha256") == _digest_ignoring_entry(dest)
        print(f"{it['name']}: present {nbytes} bytes hash {'ok' if ok else 'MISMATCH'}")
        if not ok:
            code = EXIT_FAIL
    try:
        du = shutil.disk_usage(root if os.path.exists(root) else os.path.dirname(root.rstrip('/')) or "/")
        print(f"df {root}: total {du.total} used {du.used} free {du.free}")
    except OSError as exc:
        print(f"df {root}: unavailable ({exc})")
    for ln in _quota_lines():
        print(f"quota: {ln}")
    return code


def _digest_ignoring_entry(dest: str) -> str:
    """Tree digest excluding the .entry.json written after the digest was taken."""
    if os.path.isfile(dest):
        return tree_digest(dest)
    lines = [ln for ln in trash.manifest_lines(dest) if ln.split("  ", 1)[1] != ".entry.json"]
    return hashlib.sha256("".join(ln + "\n" for ln in lines).encode()).hexdigest()


def _write_manifest(dest: str) -> str:
    if os.path.isdir(dest):
        target = os.path.join(dest, "MANIFEST.sha256")
        lines = [ln for ln in trash.manifest_lines(dest) if ln.split("  ", 1)[1] != "MANIFEST.sha256"]
    else:
        target = dest + ".MANIFEST.sha256"
        lines = [f"{trash._sha256(dest)}  {os.path.basename(dest)}"]
    Path(target).write_text("".join(ln + "\n" for ln in lines), encoding="utf-8")
    return target


def _node_trash() -> List[Dict[str, Any]]:
    host, now = audit.short_host(), datetime.now(timezone.utc)
    out = []
    for e in trash.index_state().values():
        if e.get("host") != host:
            continue
        try:
            if trash._parse_ts(e["expires_at"]) <= now:
                continue
        except (KeyError, ValueError):
            pass
        out.append(e)
    return out


def release(spec: Dict[str, Any], root: str, dry_run: bool = False) -> int:
    keeps = [o for o in spec["outputs"] if o.get("keep")]
    code = EXIT_OK
    verified: List[str] = []
    for o in keeps:
        src = _under(root, o["path"])
        dest = o["dest"]
        if dry_run:
            print(f"copy    {o['name']}: {src} -> {dest}; verify; write MANIFEST.sha256")
            verified.append(src)
            continue
        try:
            if not os.path.exists(src):
                raise NodeError(f"{o['name']}: {src} missing")
            want = tree_digest(src)
            if o.get("sha256") and want != o["sha256"]:
                raise NodeError(f"{o['name']}: node copy hash differs from spec sha256")
            if os.path.exists(dest):
                if tree_digest(dest) != want:
                    raise NodeError(f"{dest} exists with different content; not overwriting")
            else:
                part = dest.rstrip("/") + ".partial"
                if os.path.lexists(part):
                    raise NodeError(f"{part} exists (interrupted run?)")
                os.makedirs(os.path.dirname(dest.rstrip("/")) or ".", exist_ok=True)
                _copy(src, part)
                if tree_digest(part) != want:
                    raise NodeError(f"{o['name']}: copy-back verification failed ({part} left in place)")
                os.rename(part, dest)
            _write_manifest(dest)
            verified.append(src)
            print(f"released {o['name']} -> {dest}")
        except (NodeError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            code = EXIT_FAIL
    entries = _node_trash()
    print(f"unexpired node-local trash entries: {len(entries)}")
    for e in entries:
        print(f"  {e['id']}  {e.get('bytes', 0):>12}  exp {str(e.get('expires_at', ''))[:10]}  {e.get('original_path')}")
    if code != EXIT_OK:
        print("not clearing node copies: a copy-back failed")
        return code
    targets = verified + [_under(root, it["dest"]) for it in spec["items"]
                          if os.path.exists(_under(root, it["dest"]))]
    if targets:
        rc = trash.rm(targets, dry_run=dry_run)
        code = max(code, EXIT_FAIL if rc else EXIT_OK)
    return code


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="cdx_agent.node")
    ap.add_argument("cmd", choices=["setup", "status", "release"])
    ap.add_argument("--spec", default=".cdx/node_env.yaml")
    ap.add_argument("--root")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    try:
        spec = load_spec(a.spec)
        root = os.path.abspath(a.root or default_root(spec))
        if a.cmd == "setup":
            return setup(spec, root, a.dry_run)
        if a.cmd == "status":
            return status(spec, root)
        return release(spec, root, a.dry_run)
    except (NodeError, OSError, yaml.YAMLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
