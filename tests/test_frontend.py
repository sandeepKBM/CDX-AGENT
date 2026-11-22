import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASH = ROOT / "tools" / "bin" / "cdx-agent"


@pytest.fixture()
def env(tmp_path):
    """Stub python3 first on PATH: records argv, delegates to the real interpreter."""
    stubdir = tmp_path / "stub"
    stubdir.mkdir()
    log = tmp_path / "argv.log"
    stub = stubdir / "python3"
    stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{log}"\nexec "{sys.executable}" "$@"\n')
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    home = tmp_path / "home"
    home.mkdir()
    e = dict(os.environ)
    e.update({"HOME": str(home), "PATH": f"{stubdir}:{e['PATH']}", "CDX_AGENT_PYTHONPATH": str(ROOT / "src"),
              "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": str(tmp_path)})
    e.pop("CDX_DRY_RUN", None)
    return e, log, home


def run(env, *args):
    e, log, home = env
    cwd = home / "work"
    cwd.mkdir(exist_ok=True)
    return subprocess.run(["bash", str(BASH), *args], env=e, cwd=cwd, capture_output=True, text=True, timeout=60)


def calls(log):
    return log.read_text().splitlines() if log.exists() else []


def test_unknown_flag_exits_2_without_side_effects(env):
    e, log, home = env
    before = set(home.rglob("*"))
    res = run(env, "--hygeine")
    assert res.returncode == 2
    assert "unknown option --hygeine; did you mean --hygiene?" in res.stderr
    assert set(home.rglob("*")) - before <= {home / "work"}
    assert all("cdx_agent.modes" in c for c in calls(log))


def test_flag_after_separator_not_checked(env):
    res = run(env, "--dry-run", "--", "--whatever")
    assert "unknown option" not in res.stderr


def test_dry_run_on_unsupported_mode(env):
    e, log, home = env
    res = run(env, "--dry-run", "--cpu-job", "--", "true")
    assert res.returncode == 2
    assert "--cpu-job has no dry run" in res.stderr
    assert not any("cdx_agent.cpu_job" in c for c in calls(log))


def test_dry_run_env_on_unsupported_mode(env):
    e, log, home = env
    e["CDX_DRY_RUN"] = "1"
    res = run(env, "--trash", "list")
    assert res.returncode == 2
    assert "--trash has no dry run" in res.stderr
    assert not any("cdx_agent.trash" in c for c in calls(log))


def test_help_text_golden():
    from cdx_agent import modes

    res = subprocess.run([sys.executable, "-m", "cdx_agent.modes", "--help-text"], capture_output=True, text=True,
                         env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    assert res.returncode == 0
    for m in modes.MODES:
        if not m.deprecated and m.flag != "-h":
            assert m.flag in res.stdout


def test_bash_syntax():
    assert subprocess.run(["bash", "-n", str(BASH)]).returncode == 0


REMOVED_FLAGS = ["--dg", "--graperoot", "--dg-root", "--graperoot-root", "--dg-workspace", "--dg-workspace-init",
                 "--dg-workspace-list", "--dg-workspace-show", "--dg-workspace-clean",
                 "--doctor", "--token-doctor", "--usage", "--usage-session", "--usage-month"]


@pytest.mark.parametrize("flag", REMOVED_FLAGS)
def test_removed_flag_exits_2_with_suggestion(env, flag):
    e, log, home = env
    res = run(env, flag)
    assert res.returncode == 2
    assert f"unknown option {flag}" in res.stderr
    assert all("cdx_agent.modes" in c for c in calls(log))


def test_removed_doctor_suggests_session_doctor(env):
    assert "did you mean --session-doctor?" in run(env, "--doctor").stderr


def test_bash_help_has_no_removed_modes_and_new_headings(env):
    res = run(env, "--help")
    assert res.returncode == 0
    words = set(res.stdout.split())
    for flag in REMOVED_FLAGS:
        assert flag not in words
    assert "GrapeRoot" not in res.stdout and "Dual-Graph" not in res.stdout
    assert "runs codex" not in res.stdout
    assert "Jobs and helpers" in res.stdout
    for flag in ("--cpu-job", "--watch", "--gpu-pick", "--handoff", "--run", "--suite", "--claude", "--codex", "--secondary"):
        assert flag in words, flag
    jobs = res.stdout.index("Jobs and helpers")
    for flag in ("--cpu-job", "--watch", "--gpu-pick", "--handoff", "--run", "--suite"):
        assert res.stdout.index(f"  {flag}", jobs) > jobs


def test_hash_repro_and_teardown_dispatch_to_python_modules(env):
    e, log, home = env
    res = run(env, "--hash-repro", "--want")
    assert res.returncode == 2 and "--want needs a value" in res.stderr
    res = run(env, "--teardown")
    assert res.returncode == 2 and "usage: cdx-agent --teardown" in res.stderr
    logged = calls(log)
    assert any("cdx_agent.hash_repro --want" in c for c in logged)
    assert any("cdx_agent.teardown" in c for c in logged)
