"""cdx_agent.offload: pinned-luna codex exec and the local-only Codex doctor checks."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from cdx_agent import offload


def _codex_home(tmp_path: Path, slugs, model: str | None = "gpt-5.6-luna", releases=("0.163.0",)) -> Path:
    home = tmp_path / "codex_home"
    home.mkdir()
    (home / "models_cache.json").write_text(json.dumps({"models": [{"slug": s} for s in slugs]}))
    if model:
        (home / "config.toml").write_text(f'model = "{model}"\nmodel_reasoning_effort = "xhigh"\n[tui]\nx = 1\n')
    for r in releases:
        (home / "packages" / "standalone" / "releases" / f"{r}-x86_64-unknown-linux-musl" / "bin").mkdir(parents=True)
    return home


def _fake_codex(tmp_path: Path, body: str, version: str = "0.163.0") -> Path:
    """A stand-in `codex`: records argv + stdin, then runs body (bash)."""
    p = tmp_path / "bin" / "codex"
    p.parent.mkdir(exist_ok=True)
    p.write_text(
        "#!/usr/bin/env bash\n"
        f'if [[ "$1" == "--version" ]]; then echo "codex-cli {version}"; exit 0; fi\n'
        f'printf "%s\\n" "$@" > "{tmp_path}/argv.txt"\n'
        # an open, never-closed stdin makes real codex hang ("Reading additional input from stdin...")
        f'timeout 5 cat > "{tmp_path}/stdin.txt"; echo $? > "{tmp_path}/cat_rc.txt"\n'
        'out=""; prev=""; for a in "$@"; do [[ "$prev" == "-o" ]] && out="$a"; prev="$a"; done\n'
        f"{body}\n"
    )
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return p


def test_command_is_pinned_to_luna_read_only():
    cmd = offload.build_command("codex", "/repo", "/tmp/last.txt")
    assert cmd == ["codex", "exec", "-m", "gpt-5.6-luna", "--sandbox", "read-only", "--skip-git-repo-check",
                   "-C", "/repo", "-o", "/tmp/last.txt", "-"]


def test_offload_prints_only_final_answer(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CODEX_HOME", str(_codex_home(tmp_path, ["gpt-5.6-luna"])))
    codex = _fake_codex(tmp_path, 'echo "lots of transcript noise"; echo noise >&2; echo OK > "$out"')
    rc = offload.run_offload("Reply with exactly: OK", str(tmp_path), timeout=30, raw=False, codex=str(codex))
    assert rc == 0
    assert capsys.readouterr().out == "OK\n"
    argv = (tmp_path / "argv.txt").read_text().split("\n")
    assert argv[argv.index("-m") + 1] == "gpt-5.6-luna" and "read-only" in argv
    assert (tmp_path / "stdin.txt").read_text() == "Reply with exactly: OK"


def test_codex_stdin_is_closed_even_when_callers_stdin_stays_open(tmp_path):
    """Regression: codex exec with an open stdin prints 'Reading additional input from stdin...' and hangs.
    The caller here (like a background shell) keeps its stdin pipe open forever; codex must still see EOF
    right after the prompt, and nothing else."""
    home = _codex_home(tmp_path, ["gpt-5.6-luna"])
    codex = _fake_codex(tmp_path, 'echo OK > "$out"')
    env = dict(os.environ, CODEX_HOME=str(home), PATH=f"{codex.parent}{os.pathsep}{os.environ['PATH']}",
               PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    proc = subprocess.Popen([sys.executable, "-m", "cdx_agent.offload", "--repo", str(tmp_path), "Say OK"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        out, err = _wait_without_closing_stdin(proc)
    finally:
        proc.stdin.close()
    assert proc.returncode == 0, err
    assert out == "OK\n"
    assert (tmp_path / "cat_rc.txt").read_text().strip() == "0"  # EOF reached; 124 would mean it hung
    assert (tmp_path / "stdin.txt").read_text() == "Say OK"


def _wait_without_closing_stdin(proc: subprocess.Popen, timeout: float = 30.0):
    proc.wait(timeout=timeout)  # stdin deliberately left open while waiting
    return proc.stdout.read(), proc.stderr.read()


def test_offload_refuses_when_luna_missing_no_fallback(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CODEX_HOME", str(_codex_home(tmp_path, ["gpt-5.6-sol", "gpt-6-luna"])))
    codex = _fake_codex(tmp_path, 'echo SHOULD-NOT-RUN > "$out"')
    rc = offload.run_offload("hi", str(tmp_path), timeout=30, raw=False, codex=str(codex))
    assert rc == offload.EXIT_MODEL_UNAVAILABLE
    err = capsys.readouterr().err
    assert "gpt-5.6-luna" in err and "refusing to fall back" in err
    assert not (tmp_path / "argv.txt").exists()  # codex never ran


def test_offload_reports_server_model_rejection(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CODEX_HOME", str(_codex_home(tmp_path, ["gpt-5.6-luna"])))
    codex = _fake_codex(tmp_path, "echo '400 Bad Request: The model is not supported' >&2; exit 1")
    rc = offload.run_offload("hi", str(tmp_path), timeout=30, raw=False, codex=str(codex))
    assert rc == offload.EXIT_MODEL_UNAVAILABLE
    assert "no fallback" in capsys.readouterr().err


def test_offload_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(_codex_home(tmp_path, ["gpt-5.6-luna"])))
    codex = _fake_codex(tmp_path, "sleep 30")
    assert offload.run_offload("hi", str(tmp_path), timeout=0.5, raw=False, codex=str(codex)) == offload.EXIT_TIMEOUT


def test_doctor_flags_dangling_link_old_cli_and_retired_model(tmp_path, monkeypatch):
    home = _codex_home(tmp_path, ["gpt-5.6-luna", "gpt-6-sol"], model="gpt-5.4-mini", releases=("0.135.0", "0.163.0"))
    dead = tmp_path / "deadbin"
    dead.mkdir()
    os.symlink(str(tmp_path / "deleted_codex_homes" / "codex"), dead / "codex")
    good = _fake_codex(tmp_path, "true", version="0.135.0").parent
    lines = offload.doctor_lines(path_env=f"{dead}{os.pathsep}{good}", home=home)
    text = "\n".join(lines)
    assert "DANGLING" in text and str(dead / "codex") in text
    assert f"codex_bin={good / 'codex'}" in text
    assert "codex_cli_version=WARN 0.135.0 is older than the latest installed release 0.163.0" in text
    assert "codex_config_model=WARN gpt-5.4-mini not in models_cache" in text
    assert "codex_offload_model=gpt-5.6-luna available" in text


def test_doctor_ok_and_missing_cache(tmp_path):
    home = _codex_home(tmp_path, ["gpt-5.6-luna"])
    good = _fake_codex(tmp_path, "true").parent
    text = "\n".join(offload.doctor_lines(path_env=str(good), home=home))
    assert "codex_cli_version=0.163.0 ok" in text and "codex_config_model=gpt-5.6-luna ok" in text
    (home / "models_cache.json").unlink()
    text = "\n".join(offload.doctor_lines(path_env="", home=home))
    assert "codex_bin=MISSING" in text and "MISSING/unreadable" in text


def test_configured_model_honours_profile(tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    (home / "config.toml").write_text('model = "a"\nprofile = "p"\n[profiles.p]\nmodel = "b"\n')
    assert offload.configured_model(home) == "b"
