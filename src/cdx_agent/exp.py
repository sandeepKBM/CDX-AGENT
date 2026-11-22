"""Thin experiment layer.

    python -m cdx_agent.exp wrap [--name N] [--run-dir D] -- CMD
    python -m cdx_agent.exp run ENTRY [-c conf.yaml] [key=value ...] [-m] [--profile P] [--smoke] [--units-file F]
    python -m cdx_agent.exp prereg RUN_DIR [--from TEMPLATE]
    python -m cdx_agent.exp compare A B
    python -m cdx_agent.exp diff A B
    python -m cdx_agent.exp export RUN [--to DIR] [--all]
    python -m cdx_agent.exp archive RUN

Exit codes: 0 ok, 1 failure, 2 refused.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from typing import Any, Optional, Sequence

import yaml

from cdx_agent import coldbank, expconfig, manifest, run, runlog, stats

EXIT_REFUSED = 2
ENV_KEYS = ("CDX_RUN_DIR", "PYTHONPATH", "VIRTUAL_ENV", "CONDA_DEFAULT_ENV", "CUDA_VISIBLE_DEVICES", "SLURM_JOB_ID",
            "OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS")
TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools", "templates", "PREREG.md")
DEFAULT_EXPORT_ROOT = "/common/users/ss5772/downloads"
RESOLVED = "config.resolved.yaml"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-")[:40] or "run"


def _default_run_dir(repo: str, name: str) -> str:
    return os.path.join(repo, "runs", f"{time.strftime('%Y-%m-%d')}_{_slug(name)}_{secrets.token_hex(3)}")


def _env_subset() -> dict[str, str]:
    return {k: os.environ[k] for k in ENV_KEYS if k in os.environ}


def _atomic_text(path: str, text: str) -> None:
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _index(repo: str) -> str:
    return os.path.join(repo, ".cdx", "jobs.jsonl")


def _start_manifest(run_dir: str, repo: str, name: str, command: list[str], **extra: Any) -> None:
    manifest.write(
        run_dir, index=_index(repo), job_id=os.path.basename(run_dir.rstrip("/")), name=name,
        project=os.path.basename(repo), command=command, cwd=os.getcwd(), code=run.git_state(repo), env=_env_subset(),
        host=os.uname().nodename, started=_now(), state="running", **extra)


def _finish(run_dir: str, rc: int, state: Optional[str] = None) -> None:
    state = state or ("done" if rc == 0 else "failed")
    manifest.update(run_dir, finished=_now(), exit_code=rc, state=state)
    _atomic_text(os.path.join(run_dir, "DONE.json"),
                 json.dumps({"job_id": os.path.basename(run_dir.rstrip("/")), "exit_code": rc, "finished": _now(),
                             "host": os.uname().nodename}) + "\n")


# --------------------------------------------------------------------------- wrap
def cmd_wrap(args: argparse.Namespace, cmd: list[str]) -> int:
    if not cmd:
        print("exp wrap: no command (use: -- CMD ...)", file=sys.stderr)
        return EXIT_REFUSED
    repo = run.repo_root(os.getcwd())
    name = args.name or _slug(os.path.basename(cmd[0]))
    run_dir = os.path.abspath(args.run_dir or _default_run_dir(repo, name))
    os.makedirs(os.path.join(run_dir, "logs"), exist_ok=True)
    _start_manifest(run_dir, repo, name, cmd)
    log = runlog.RunLog(run_dir)
    log.start(name=name, command=cmd)
    env = dict(os.environ, CDX_RUN_DIR=run_dir)
    with open(os.path.join(run_dir, "logs", "stdout.log"), "ab") as out:
        try:
            rc = subprocess.run(cmd, env=env, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL).returncode
        except OSError as exc:
            out.write(f"exp wrap: {exc}\n".encode())
            rc = 127
    log.end("done" if rc == 0 else "failed", exit_code=rc)
    _finish(run_dir, rc)
    print(f"run_dir={run_dir} exit_code={rc}")
    return rc


# --------------------------------------------------------------------------- run
def _entry_argv(entry: str) -> list[str]:
    argv = shlex.split(entry)
    if argv and argv[0].endswith(".py"):
        argv = [sys.executable] + argv
    return argv


def _last_json_line(text: str) -> Optional[dict[str, Any]]:
    for line in reversed(text.strip().splitlines()):
        try:
            v = json.loads(line)
        except ValueError:
            continue
        if isinstance(v, dict):
            return v
    return None


def _read_units(path: Optional[str]) -> list[str]:
    if not path:
        return ["0"]
    with open(path, encoding="utf-8") as fh:
        return [ln.strip() for ln in fh if ln.strip() and not ln.lstrip().startswith("#")]


def _collect_results(run_dir: str) -> None:
    udir = os.path.join(run_dir, "units")
    rows = []
    if os.path.isdir(udir):
        for f in sorted(os.listdir(udir)):
            if f.endswith(".json"):
                try:
                    with open(os.path.join(udir, f), encoding="utf-8") as fh:
                        rows.append(json.load(fh))
                except ValueError:
                    continue
    _atomic_text(os.path.join(run_dir, "results.jsonl"), "".join(json.dumps(r) + "\n" for r in rows))


def _run_units(run_dir: str, repo: str, entry: str, name: str, conf: dict[str, Any], overrides: list[str],
               units: list[str], world_hash: Optional[str], smoke: bool, command: list[str]) -> int:
    os.makedirs(os.path.join(run_dir, "units"), exist_ok=True)
    os.makedirs(os.path.join(run_dir, "logs"), exist_ok=True)
    _atomic_text(os.path.join(run_dir, RESOLVED), yaml.safe_dump(conf, sort_keys=True))
    extra: dict[str, Any] = {"entry": entry, "overrides": overrides, "config_hash": expconfig.config_hash(conf),
                             "world_set_hash": world_hash, "smoke": smoke}
    if os.path.exists(os.path.join(run_dir, manifest.FILENAME)):
        manifest.update(run_dir, state="running", **extra)
    else:
        _start_manifest(run_dir, repo, name, command, **extra)
    if smoke:
        units = units[:2]
    log = runlog.RunLog(run_dir)
    log.start(name=name, entry=entry, units=len(units), smoke=smoke)
    env = dict(os.environ, CDX_RUN_DIR=run_dir)
    failed = 0
    for uid in units:
        upath = os.path.join(run_dir, "units", f"{uid}.json")
        if os.path.exists(upath):
            log.unit(uid, state="skipped")
            continue
        argv = _entry_argv(entry) + ["--unit", uid, "--run-dir", run_dir, "--config", os.path.join(run_dir, RESOLVED)]
        r = subprocess.run(argv, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        with open(os.path.join(run_dir, "logs", "stdout.log"), "a", encoding="utf-8") as fh:
            fh.write(f"## unit {uid} rc={r.returncode}\n{r.stdout}{r.stderr}")
        if r.returncode == 0 and not os.path.exists(upath):
            rec = _last_json_line(r.stdout)
            if rec is not None:
                rec.setdefault("unit", uid)
                _atomic_text(upath, json.dumps(rec) + "\n")
        if r.returncode == 0:
            log.unit(uid, state="done")
        else:
            failed += 1
            log.unit(uid, state="failed", exit_code=r.returncode)
    _collect_results(run_dir)
    rc = 1 if failed else 0
    log.end("done" if rc == 0 else "failed", exit_code=rc, failed=failed)
    _finish(run_dir, rc)
    return rc


def _place(profile: str, name: str, inner: list[str]) -> Optional[int]:
    """None -> run here; otherwise the exit code of the command executed through ``cdx-agent --run``."""
    cwd = os.getcwd()
    repo = run.repo_root(cwd)
    ns = run.build_parser().parse_args(["--profile", profile, "--name", name])
    try:
        spec = run.load_spec(repo)
        req = run.build_request(ns, spec, inner, cwd)
    except (KeyError, ValueError, OSError) as exc:
        print(f"exp run: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    result = run.resolve(req, spec, run.load_site(repo), run.fetch_live(req, os.environ.get("USER", ""), None))
    if isinstance(result, run.Rejections):
        print("\n".join(run.plan_lines(result)), file=sys.stderr)
        return EXIT_REFUSED
    if run.is_local(result.cls):
        return None
    argv = ["cdx-agent", "--run", "--profile", profile, "--name", name, "--"] + inner
    print("exp run: " + " ".join(shlex.quote(x) for x in argv), flush=True)
    return subprocess.run(argv).returncode


def cmd_run(args: argparse.Namespace) -> int:
    repo = run.repo_root(os.getcwd())
    try:
        conf = expconfig.load(args.config) if args.config else {}
        expconfig.bounds(conf)
        points = expconfig.sweep(args.overrides) if args.sweep else [list(args.overrides)]
        confs = [expconfig.resolve(conf, p) for p in points]
        for c in confs:
            expconfig.bounds(c)
        units = _read_units(args.units_file)
    except (ValueError, OSError, yaml.YAMLError) as exc:
        print(f"exp run: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    world_hash = _sha256_file(args.units_file) if args.units_file else None
    name = args.name or _slug(os.path.basename(shlex.split(args.entry)[-1]).rsplit(".", 1)[0])
    base = os.path.abspath(args.run_dir or _default_run_dir(repo, name))
    rc_all = 0
    for i, (ov, c) in enumerate(zip(points, confs)):
        run_dir = f"{base}_{i:02d}" if len(points) > 1 else base
        if not args.smoke and not os.path.exists(os.path.join(run_dir, "PREREG.md")):
            os.makedirs(run_dir, exist_ok=True)
            print(f"exp run: {run_dir}/PREREG.md is missing; write it first: "
                  f"python -m cdx_agent.exp prereg {shlex.quote(run_dir)}   (or use --smoke)", file=sys.stderr)
            return EXIT_REFUSED
        new = expconfig.new_assumed(c, repo)
        if new:
            print("exp run: new ASSUMED bounds not in the ledger: " + ", ".join(new), file=sys.stderr)
        if args.profile:
            inner = [sys.executable, "-m", "cdx_agent.exp", "run", args.entry, "--run-dir", run_dir, "--name", name]
            if args.config:
                inner += ["-c", os.path.abspath(args.config)]
            if args.units_file:
                inner += ["--units-file", os.path.abspath(args.units_file)]
            if args.smoke:
                inner.append("--smoke")
            inner += ov
            placed = _place(args.profile, name, inner)
            if placed is not None:
                rc_all = rc_all or placed
                continue
        rc = _run_units(run_dir, repo, args.entry, name, c, ov, units, world_hash, args.smoke,
                        ["exp", "run", args.entry] + ov)
        print(f"run_dir={run_dir} exit_code={rc}")
        rc_all = rc_all or rc
    return rc_all


# --------------------------------------------------------------------------- prereg
def cmd_prereg(args: argparse.Namespace) -> int:
    run_dir = args.run_dir
    target = os.path.join(run_dir, "PREREG.md")
    if os.path.exists(target):
        print(f"exp prereg: {target} exists; not overwriting", file=sys.stderr)
        return EXIT_REFUSED
    tpl = args.template or TEMPLATE
    try:
        with open(tpl, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        print(f"exp prereg: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    try:
        m = manifest.read(run_dir)
    except (OSError, ValueError):
        m = {}
    fills: dict[str, str] = {}
    if m.get("world_set_hash"):
        fills["World set"] = f"sha256: {m['world_set_hash']}"
    try:
        with open(os.path.join(run_dir, RESOLVED), encoding="utf-8") as fh:
            conf = yaml.safe_load(fh) or {}
        bl = [f"{k}: {b['value']} [{b['tag']}] {b.get('source') or b.get('on') or ''}".rstrip()
              for k, b in expconfig.bounds(conf).items()]
        if bl:
            fills["Parameter box (with tags)"] = "\n".join(bl)
        if "perception" in conf:
            fills["Perception setting"] = str(conf["perception"])
        if "seeds" in conf:
            fills["Seeds"] = str(conf["seeds"])
    except (OSError, ValueError, yaml.YAMLError):
        pass
    if fills:
        parts = re.split(r"(?m)^(## .*)$", text)
        out = [parts[0]]
        for i in range(1, len(parts), 2):
            head, body = parts[i], parts[i + 1]
            key = head[3:].strip()
            out += [head, ("\n" + fills[key] + "\n\n") if key in fills else body]
        text = "".join(out)
    os.makedirs(run_dir, exist_ok=True)
    _atomic_text(target, text)
    print(target)
    return 0


# --------------------------------------------------------------------------- compare / diff
def _results(run_dir: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    p = os.path.join(run_dir, "results.jsonl")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as fh:
            for ln in fh:
                if ln.strip():
                    try:
                        v = json.loads(ln)
                    except ValueError:
                        continue
                    if isinstance(v, dict):
                        rows.append(v)
    return rows


def _conf(run_dir: str) -> dict[str, Any]:
    try:
        with open(os.path.join(run_dir, RESOLVED), encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError):
        return {}


def _man(run_dir: str) -> dict[str, Any]:
    try:
        return manifest.read(run_dir)
    except (OSError, ValueError):
        return {}


def _flat(d: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(d, dict) and d:
        out: dict[str, Any] = {}
        for k, v in d.items():
            out.update(_flat(v, f"{prefix}.{k}" if prefix else str(k)))
        return out
    return {prefix: d}


def _metric_lines(rows: list[dict[str, Any]]) -> list[tuple[str, str]]:
    keys: list[str] = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    out = []
    for k in keys:
        vals = [r[k] for r in rows if k in r]
        if vals and all(isinstance(v, bool) for v in vals):
            kk, n = sum(vals), len(vals)
            lo, hi = stats.clopper_pearson(kk, n)
            out.append((k, f"success {kk}/{n}  95% CP [{lo:.4f}, {hi:.4f}]"))
        elif vals and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
            q = stats.quantiles(vals)
            out.append((k, f"p50 {q['p50']:.4g}  p95 {q['p95']:.4g}  max over n={q['n']}: {q['max']:.4g}"))
    return out


def cmd_compare(args: argparse.Namespace) -> int:
    ma, mb = _man(args.a), _man(args.b)
    if ma.get("world_set_hash") != mb.get("world_set_hash"):
        print(f"exp compare: refused, world_set_hash differs ({ma.get('world_set_hash')} vs {mb.get('world_set_hash')})",
              file=sys.stderr)
        return EXIT_REFUSED
    ca, cb = _conf(args.a), _conf(args.b)
    if ca.get("perception") != cb.get("perception"):
        print(f"exp compare: refused, perception setting differs ({ca.get('perception')!r} vs {cb.get('perception')!r})",
              file=sys.stderr)
        return EXIT_REFUSED
    for label, d in (("A", args.a), ("B", args.b)):
        print(f"== {label}: {d}")
        rows = _results(d)
        if not rows:
            print("  no results")
        for k, line in _metric_lines(rows):
            print(f"  {k}: {line}")
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    ma, mb = _man(args.a), _man(args.b)
    fa, fb = _flat(_conf(args.a)), _flat(_conf(args.b))
    print("config:")
    diffs = [k for k in sorted(set(fa) | set(fb)) if fa.get(k, "<absent>") != fb.get(k, "<absent>")]
    for k in diffs:
        print(f"  {k}: {fa.get(k, '<absent>')!r} -> {fb.get(k, '<absent>')!r}")
    if not diffs:
        print("  (same)")
    print("code:")
    for k in ("head", "diff_sha256"):
        x, y = (ma.get("code") or {}).get(k), (mb.get("code") or {}).get(k)
        print(f"  {k}: {x}" + (f" -> {y}" if x != y else "  (same)"))
    print("env:")
    ea, eb = ma.get("env") or {}, mb.get("env") or {}
    ek = [k for k in sorted(set(ea) | set(eb)) if ea.get(k) != eb.get(k)]
    for k in ek:
        print(f"  {k}: {ea.get(k, '<absent>')!r} -> {eb.get(k, '<absent>')!r}")
    if not ek:
        print("  (same)")
    x, y = ma.get("world_set_hash"), mb.get("world_set_hash")
    print(f"world_set_hash: {x}" + (f" -> {y}" if x != y else "  (same)"))
    return 0


# --------------------------------------------------------------------------- export / archive
def _hidden_destination(dest: str) -> bool:
    """True when the resolved destination has a hidden component (`.claude`, `.tmp`, worktrees ...).

    CDX_EXPORT_TRUSTED_PREFIX (tests only) exempts the components of that prefix itself.
    """
    real = os.path.realpath(dest)
    trusted = os.environ.get("CDX_EXPORT_TRUSTED_PREFIX")
    if trusted:
        t = os.path.realpath(trusted)
        if real == t or real.startswith(t + os.sep):
            real = real[len(t):]
    return any(part.startswith(".") for part in real.split(os.sep) if part)


def cmd_export(args: argparse.Namespace) -> int:
    run_dir = os.path.abspath(args.run)
    m = _man(run_dir)
    name = _slug(m.get("name") or os.path.basename(run_dir))
    project = os.path.basename(str((m.get("code") or {}).get("repo") or "").rstrip("/")) or "experiments"
    dest = args.to or os.path.join(DEFAULT_EXPORT_ROOT, project, f"{time.strftime('%Y-%m-%d')}_{name}")
    if _hidden_destination(dest):
        print(f"exp export: refusing hidden destination {dest} (user downloads go to visible folders only)",
              file=sys.stderr)
        return EXIT_REFUSED
    files: list[str] = []
    outs = [o.get("path") if isinstance(o, dict) else o for o in (m.get("outputs") or [])]
    if outs and not args.all:
        files = [p for p in outs if p]
    else:
        for root, dirs, fs in os.walk(run_dir):
            rel_root = os.path.relpath(root, run_dir)
            if not args.all and rel_root.split(os.sep)[0] in ("logs", "units"):
                dirs[:] = []
                continue
            for f in sorted(fs):
                rel = os.path.normpath(os.path.join(rel_root, f))
                if f.startswith(".") or ".tmp." in f:
                    continue
                files.append(rel)
        files.sort()
    os.makedirs(dest, exist_ok=True)
    lines = []
    for i, rel in enumerate(files, 1):
        src = rel if os.path.isabs(rel) else os.path.join(run_dir, rel)
        if not os.path.isfile(src):
            print(f"exp export: skipping missing {rel}", file=sys.stderr)
            continue
        out = f"{i:02d}_{rel.replace(os.sep, '__')}"
        shutil.copy2(src, os.path.join(dest, out))
        lines.append(f"{_sha256_file(os.path.join(dest, out))}  {out}\n")
    _atomic_text(os.path.join(dest, "MANIFEST.sha256"), "".join(lines))
    print(f"exported {len(lines)} files to {dest}")
    return 0


def _coldbank_put(source: str, name: str, meta: list[str]) -> int:
    argv = ["put", source, name]
    for x in meta:
        argv += ["--meta", x]
    return coldbank.main(argv)


def cmd_archive(args: argparse.Namespace) -> int:
    run_dir = os.path.abspath(args.run)
    m = _man(run_dir)
    if not m:
        print(f"exp archive: no manifest in {run_dir}", file=sys.stderr)
        return EXIT_REFUSED
    name = _slug(m.get("job_id") or os.path.basename(run_dir))
    code = m.get("code") or {}
    meta = [f"source={m.get('project') or ''}:{' '.join(m.get('command') or [])}".strip(),
            f"owner={os.environ.get('USER', 'unknown')}", "regenerable=yes",
            f"cost={m.get('started') or ''}..{m.get('finished') or ''}",
            f"head={code.get('head')}", f"world_set_hash={m.get('world_set_hash')}"]
    meta = [x.replace("\n", " ") for x in meta]
    return _coldbank_put(run_dir, name, meta)


# --------------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cdx_agent.exp", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("wrap")
    w.add_argument("--name")
    w.add_argument("--run-dir")
    r = sub.add_parser("run")
    r.add_argument("entry")
    r.add_argument("overrides", nargs="*", help="key=value overrides")
    r.add_argument("-c", "--config")
    r.add_argument("-m", "--sweep", action="store_true")
    r.add_argument("--profile")
    r.add_argument("--smoke", action="store_true")
    r.add_argument("--units-file")
    r.add_argument("--name")
    r.add_argument("--run-dir")
    g = sub.add_parser("prereg")
    g.add_argument("run_dir")
    g.add_argument("--from", dest="template")
    for n in ("compare", "diff"):
        c = sub.add_parser(n)
        c.add_argument("a")
        c.add_argument("b")
    e = sub.add_parser("export")
    e.add_argument("run")
    e.add_argument("--to")
    e.add_argument("--all", action="store_true")
    a = sub.add_parser("archive")
    a.add_argument("run")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, cmd = argv[:i], argv[i + 1:]
    parser = build_parser()
    args, extra = parser.parse_known_args(argv)
    if extra:
        if args.cmd == "run" and all("=" in x and not x.startswith("-") for x in extra):
            args.overrides = list(args.overrides) + extra  # key=value words that followed an option
        else:
            parser.error("unrecognized arguments: " + " ".join(extra))
    if args.cmd == "wrap":
        return cmd_wrap(args, cmd)
    return {"run": cmd_run, "prereg": cmd_prereg, "compare": cmd_compare, "diff": cmd_diff, "export": cmd_export,
            "archive": cmd_archive}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
