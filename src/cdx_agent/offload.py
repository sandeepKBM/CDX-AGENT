"""Non-interactive Codex reasoning offload, pinned to one model, plus local-only Codex health checks.

    python -m cdx_agent.offload [--repo DIR] [--timeout S] [--raw] "<prompt>"     (or the prompt on stdin)
    python -m cdx_agent.offload --doctor

Offload runs  codex exec -m gpt-5.6-luna --sandbox read-only --skip-git-repo-check -C <repo> -o <tmp> -
with the prompt on stdin, and prints only the final answer (--raw streams codex's full output instead).
codex never inherits the caller's stdin: it gets a pipe carrying only the prompt, closed right after
(an open stdin makes `codex exec` print "Reading additional input from stdin..." and hang).
The model is PINNED (user rule: always luna). If luna is missing from the local model cache, or codex rejects
it, this fails with a clear message and never falls back to another model.

--doctor prints key=value lines (appended to `cdx-agent --doctor`) and reads local files only: no network,
and it never runs `codex update`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

PINNED_MODEL = "gpt-5.6-luna"
EXIT_MODEL_UNAVAILABLE = 3
EXIT_TIMEOUT = 124

_MODEL_ERROR_PATTERNS = (
    r"model is not supported",
    r"model_not_found",
    r"does not exist or you do not have access",
    r"unknown variant",
    r"unsupported model",
    r"invalid model",
)


# --------------------------------------------------------------------------- local codex facts
def codex_home() -> Path:
    env = os.environ.get("CODEX_HOME")
    return Path(env) if env else Path.home() / ".codex"


def parse_version(text: str) -> Optional[Tuple[int, ...]]:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(x) for x in m.groups()) if m else None


def fmt_version(v: Optional[Tuple[int, ...]]) -> str:
    return ".".join(str(x) for x in v) if v else "?"


def model_cache_slugs(home: Optional[Path] = None) -> Optional[List[str]]:
    """Slugs from <codex_home>/models_cache.json, or None when the cache is missing/unreadable."""
    path = (home or codex_home()) / "models_cache.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    models = data.get("models") if isinstance(data, dict) else data
    if not isinstance(models, list):
        return None
    return [str(m.get("slug")) for m in models if isinstance(m, dict) and m.get("slug")]


def configured_model(home: Optional[Path] = None) -> Optional[str]:
    """Top-level `model` of config.toml, honouring a top-level `profile = "x"` with [profiles.x].model."""
    path = (home or codex_home()) / "config.toml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        try:
            import tomllib  # type: ignore[import-not-found]
        except ModuleNotFoundError:  # pragma: no cover - python < 3.11
            import tomli as tomllib  # type: ignore[no-redef]
        data = tomllib.loads(text)
        profile = data.get("profile")
        if profile and isinstance(data.get("profiles"), dict):
            prof = data["profiles"].get(profile) or {}
            if prof.get("model"):
                return str(prof["model"])
        return str(data["model"]) if data.get("model") else None
    except Exception:  # unparsable config: fall back to the first top-level model line
        for line in text.splitlines():
            if line.lstrip().startswith("["):
                break
            m = re.match(r"\s*model\s*=\s*[\"']([^\"']+)[\"']", line)
            if m:
                return m.group(1)
        return None


def installed_releases(home: Optional[Path] = None) -> List[Tuple[Tuple[int, ...], str]]:
    root = (home or codex_home()) / "packages" / "standalone" / "releases"
    out = []
    try:
        for entry in root.iterdir():
            v = parse_version(entry.name)
            if v and entry.is_dir():
                out.append((v, entry.name))
    except OSError:
        pass
    return sorted(out)


def path_codex_entries(path_env: Optional[str] = None) -> List[Tuple[str, Optional[str], bool]]:
    """Every `codex` on PATH, including dangling symlinks that shutil.which() silently skips:
    (path, resolved target or None, usable)."""
    seen, out = set(), []
    for d in (path_env if path_env is not None else os.environ.get("PATH", "")).split(os.pathsep):
        if not d:
            continue
        p = os.path.join(d, "codex")
        if p in seen or not os.path.lexists(p):
            continue
        seen.add(p)
        real = os.path.realpath(p)
        usable = os.path.isfile(real) and os.access(real, os.X_OK)
        out.append((p, real if os.path.lexists(real) else None, usable))
    return out


def codex_cli_version(binary: str, timeout: float = 15.0) -> Tuple[Optional[str], str]:
    try:
        r = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"failed: {exc}"
    text = (r.stdout or r.stderr).strip()
    return (text if r.returncode == 0 else None), text


def doctor_lines(path_env: Optional[str] = None, home: Optional[Path] = None) -> List[str]:
    home = home or codex_home()
    lines: List[str] = []
    entries = path_codex_entries(path_env)
    usable = [e for e in entries if e[2]]
    for p, real, ok in entries:
        if not ok:
            target = os.readlink(p) if os.path.islink(p) else p
            lines.append(f"codex_bin_check=WARN {p} -> {target} is DANGLING/not executable (fix the link or PATH)")
    if not usable:
        lines.append("codex_bin=MISSING (no usable `codex` on PATH; expected ~/.local/bin/codex -> "
                     "~/.codex/packages/standalone/current/bin/codex)")
        binary = None
    else:
        binary, real, _ = usable[0]
        lines.append(f"codex_bin={binary} -> {real} ok")
    if binary:
        ver_text, detail = codex_cli_version(binary)
        if ver_text is None:
            lines.append(f"codex_cli_version=FAIL `codex --version` did not run: {detail[:200]}")
        else:
            cur = parse_version(ver_text)
            rel = installed_releases(home)
            latest = rel[-1] if rel else None
            if latest and cur and cur < latest[0]:
                lines.append(f"codex_cli_version=WARN {fmt_version(cur)} is older than the latest installed release "
                             f"{latest[1]} (old CLIs fail to parse the server model list, e.g. 0.135.0 "
                             f"'unknown variant max'); repoint {home}/packages/standalone/current")
            else:
                lines.append(f"codex_cli_version={fmt_version(cur)} ok (latest installed release "
                             f"{latest[1] if latest else 'none found'})")
    slugs = model_cache_slugs(home)
    model = configured_model(home)
    cache = home / "models_cache.json"
    if slugs is None:
        lines.append(f"codex_models_cache={cache} MISSING/unreadable (cannot check model availability offline)")
    else:
        lines.append(f"codex_models_cache={cache} {len(slugs)} models")
        if model is None:
            lines.append(f"codex_config_model=none set in {home}/config.toml (codex default)")
        elif model in slugs:
            lines.append(f"codex_config_model={model} ok (in models_cache)")
        else:
            lines.append(f"codex_config_model=WARN {model} not in models_cache slugs (retired? e.g. gpt-5.4-mini "
                         f"-> 400 'model is not supported'); available: {','.join(slugs)}")
        lines.append(f"codex_offload_model={PINNED_MODEL} "
                     f"{'available' if PINNED_MODEL in slugs else 'NOT AVAILABLE (cdx-agent --offload will refuse)'}")
    return lines


# --------------------------------------------------------------------------- offload
def build_command(codex: str, repo: str, last_message_file: str) -> List[str]:
    return [codex, "exec", "-m", PINNED_MODEL, "--sandbox", "read-only", "--skip-git-repo-check",
            "-C", repo, "-o", last_message_file, "-"]


def looks_like_model_error(text: str) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in _MODEL_ERROR_PATTERNS)


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.wait()


def run_offload(prompt: str, repo: str, timeout: float, raw: bool, codex: Optional[str] = None) -> int:
    codex = codex or shutil.which("codex")
    if not codex:
        print("offload: no usable `codex` on PATH (run `cdx-agent --doctor`)", file=sys.stderr)
        return 1
    slugs = model_cache_slugs()
    if slugs is not None and PINNED_MODEL not in slugs:
        print(f"offload: {PINNED_MODEL} is not in {codex_home()}/models_cache.json (available: {','.join(slugs)}). "
              f"The model is pinned; refusing to fall back to another model.", file=sys.stderr)
        return EXIT_MODEL_UNAVAILABLE
    if not os.path.isdir(repo):
        print(f"offload: --repo {repo} is not a directory", file=sys.stderr)
        return 1
    tmp = tempfile.mkdtemp(prefix="cdx_offload_")
    last = os.path.join(tmp, "last_message.txt")
    out_path, err_path = os.path.join(tmp, "stdout.txt"), os.path.join(tmp, "stderr.txt")
    cmd = build_command(codex, os.path.abspath(repo), last)
    try:
        with open(out_path, "w") as out_fh, open(err_path, "w") as err_fh:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, text=True, start_new_session=True,
                                    stdout=None if raw else out_fh, stderr=None if raw else err_fh)
            try:
                proc.communicate(prompt, timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_group(proc)
                print(f"offload: codex exec timed out after {timeout:.0f} s", file=sys.stderr)
                return EXIT_TIMEOUT
            except KeyboardInterrupt:
                _kill_group(proc)
                return 130
        rc = proc.returncode
        logs = ""
        if not raw:
            logs = Path(out_path).read_text(errors="replace") + Path(err_path).read_text(errors="replace")
        if rc != 0:
            if looks_like_model_error(logs):
                print(f"offload: codex rejected the pinned model {PINNED_MODEL} (no fallback). "
                      f"Check `cdx-agent --doctor`; the CLI may need updating.", file=sys.stderr)
            print(f"offload: codex exec failed rc={rc}", file=sys.stderr)
            if logs:
                print("\n".join(logs.strip().splitlines()[-15:]), file=sys.stderr)
            return EXIT_MODEL_UNAVAILABLE if looks_like_model_error(logs) else rc
        if not raw:
            answer = Path(last).read_text(errors="replace").strip() if os.path.exists(last) else ""
            if not answer:
                print("offload: codex exec returned no final message", file=sys.stderr)
                print("\n".join(logs.strip().splitlines()[-15:]), file=sys.stderr)
                return 1
            print(answer)
        return 0
    finally:
        for p in (last, out_path, err_path):
            try:
                os.remove(p)
            except OSError:
                pass
        try:
            os.rmdir(tmp)
        except OSError:
            pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cdx-agent --offload", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("prompt", nargs="*", help="prompt text (or '-' / omitted: read from stdin)")
    p.add_argument("--repo", default=".", help="directory codex works in (codex exec -C; default: cwd)")
    p.add_argument("--timeout", type=float, default=900.0, help="seconds before codex is killed (default 900)")
    p.add_argument("--raw", action="store_true", help="stream codex's full output instead of only the final answer")
    p.add_argument("--doctor", action="store_true", help="print local Codex health checks and exit")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.doctor:
        for line in doctor_lines():
            print(line)
        return 0
    prompt = " ".join(args.prompt).strip()
    if not prompt or prompt == "-":
        if sys.stdin.isatty():
            print("offload: give a prompt argument or pipe one on stdin", file=sys.stderr)
            return 1
        prompt = sys.stdin.read().strip()
    if not prompt:
        print("offload: empty prompt", file=sys.stderr)
        return 1
    return run_offload(prompt, args.repo, args.timeout, args.raw)


if __name__ == "__main__":
    raise SystemExit(main())
