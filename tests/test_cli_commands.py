import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cdx_agent.cli import main


def _write_stub_binary(bin_dir: Path, name: str, marker_dir: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / name
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys, json\n"
        f"marker = {str(marker_dir / (name + '_invocation.json'))!r}\n"
        "payload = {'argv': sys.argv[1:], 'cwd': os.getcwd()}\n"
        "open(marker, 'w').write(json.dumps(payload))\n"
        "sys.exit(0)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture()
def cli_env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".codex").mkdir()
    (home / ".codex" / "config.toml").write_text('model = "gpt-5.4-mini"\n')
    (home / ".codex" / "auth.json").write_text('{"token": "fake"}')
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text("{}")
    monkeypatch.setattr("pathlib.Path.home", staticmethod(lambda: home))
    monkeypatch.delenv("CDX_AGENT_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-such-config"))

    repo = tmp_path / "repo"
    repo.mkdir()
    return {"home": home, "repo": repo, "tmp_path": tmp_path}


def test_launch_dry_run_via_cli(cli_env, capsys):
    exit_code = main(["launch", "--repo", str(cli_env["repo"]), "--engine", "codex", "--safe", "--dry-run"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "ENGINE=codex" in out
    assert "DRY_RUN_COMMAND=" in out


def test_launch_token_saver_flag_keeps_block_via_cli(cli_env, monkeypatch, capsys):
    from cdx_agent import config as config_mod
    from cdx_agent import context_docs

    cfg = config_mod.load_config()
    base_agents = cfg.tools_root / "base" / "AGENTS.md"
    base_agents.parent.mkdir(parents=True, exist_ok=True)
    base_agents.write_text(
        f"{context_docs.TOKEN_SAVER_START_MARKER}\nsaver text\n{context_docs.TOKEN_SAVER_END_MARKER}\n"
    )

    marker_dir = cli_env["tmp_path"] / "markers"
    marker_dir.mkdir()
    stub_bin = cli_env["tmp_path"] / "stub_bin"
    _write_stub_binary(stub_bin, "codex", marker_dir)
    monkeypatch.setenv("PATH", f"{stub_bin}{os.pathsep}{os.environ['PATH']}")

    exit_code = main(["launch", "--repo", str(cli_env["repo"]), "--engine", "codex", "--safe", "--token-saver"])
    assert exit_code == 0
    capsys.readouterr()

    from cdx_agent import runtime as runtime_mod

    repo = config_mod.repo_root(cli_env["repo"])
    rctx = runtime_mod.runtime_context(cfg, repo, access_mode="safe", engine="codex")
    assert "saver text" in rctx.agents_path.read_text()


def test_launch_default_omits_token_saver_block_via_cli(cli_env, monkeypatch, capsys):
    from cdx_agent import config as config_mod
    from cdx_agent import context_docs

    cfg = config_mod.load_config()
    base_agents = cfg.tools_root / "base" / "AGENTS.md"
    base_agents.parent.mkdir(parents=True, exist_ok=True)
    base_agents.write_text(
        f"{context_docs.TOKEN_SAVER_START_MARKER}\nsaver text\n{context_docs.TOKEN_SAVER_END_MARKER}\n"
    )

    marker_dir = cli_env["tmp_path"] / "markers"
    marker_dir.mkdir()
    stub_bin = cli_env["tmp_path"] / "stub_bin"
    _write_stub_binary(stub_bin, "codex", marker_dir)
    monkeypatch.setenv("PATH", f"{stub_bin}{os.pathsep}{os.environ['PATH']}")

    exit_code = main(["launch", "--repo", str(cli_env["repo"]), "--engine", "codex", "--safe"])
    assert exit_code == 0
    capsys.readouterr()

    from cdx_agent import runtime as runtime_mod

    repo = config_mod.repo_root(cli_env["repo"])
    rctx = runtime_mod.runtime_context(cfg, repo, access_mode="safe", engine="codex")
    assert "saver text" not in rctx.agents_path.read_text()


def test_claude_shorthand_dry_run_via_cli(cli_env, capsys):
    exit_code = main(["--claude", "--repo", str(cli_env["repo"]), "--dry-run"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "ENGINE=claude" in out


def test_codex_shorthand_dry_run_via_cli(cli_env, capsys):
    # The one-flag way back to codex now that claude is the default engine;
    # bash's arg parser has had --codex since the cutover, the python CLI
    # previously only had --claude.
    exit_code = main(["--codex", "--repo", str(cli_env["repo"]), "--dry-run"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "ENGINE=codex" in out


def test_launch_real_invokes_stub_binary_via_cli(cli_env, monkeypatch, capsys):
    marker_dir = cli_env["tmp_path"] / "markers"
    marker_dir.mkdir()
    stub_bin = cli_env["tmp_path"] / "stub_bin"
    _write_stub_binary(stub_bin, "codex", marker_dir)
    monkeypatch.setenv("PATH", f"{stub_bin}{os.pathsep}{os.environ['PATH']}")

    exit_code = main(["launch", "--repo", str(cli_env["repo"]), "--engine", "codex", "--safe"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "RUNTIME_DIR=" in out

    import json

    payload = json.loads((marker_dir / "codex_invocation.json").read_text())
    assert payload["cwd"] == str(cli_env["repo"].resolve())


def test_launch_secondary_joins_without_killing_primary_via_cli(cli_env, monkeypatch, capsys):
    from cdx_agent import config as config_mod
    from cdx_agent import runtime as runtime_mod
    from cdx_agent import session

    marker_dir = cli_env["tmp_path"] / "markers"
    marker_dir.mkdir()
    stub_bin = cli_env["tmp_path"] / "stub_bin"
    _write_stub_binary(stub_bin, "codex", marker_dir)
    monkeypatch.setenv("PATH", f"{stub_bin}{os.pathsep}{os.environ['PATH']}")

    cfg = config_mod.load_config()
    repo = config_mod.repo_root(cli_env["repo"])
    rctx = runtime_mod.runtime_context(cfg, repo, access_mode="safe", engine="codex")
    primary_handle = session.acquire_lock(session.lock_path_for(rctx.runtime_dir))
    try:
        exit_code = main(["launch", "--repo", str(cli_env["repo"]), "--engine", "codex", "--safe", "--secondary"])
        assert exit_code == 0
        # primary lock must be untouched -- still recorded as our own held lock
        assert session.read_lock_pid(session.lock_path_for(rctx.runtime_dir)) == primary_handle.pid
    finally:
        session.release_lock(primary_handle)


def test_launch_refuses_when_lock_held_without_cancel_flag(cli_env, capsys):
    from cdx_agent import config as config_mod
    from cdx_agent import runtime as runtime_mod
    from cdx_agent import session

    cfg = config_mod.load_config()
    repo = config_mod.repo_root(cli_env["repo"])
    rctx = runtime_mod.runtime_context(cfg, repo, access_mode="safe", engine="codex")
    handle = session.acquire_lock(session.lock_path_for(rctx.runtime_dir))
    try:
        exit_code = main(["launch", "--repo", str(cli_env["repo"]), "--engine", "codex", "--safe"])
        assert exit_code == 1
        err = capsys.readouterr().err
        assert "session appears active" in err
    finally:
        session.release_lock(handle)


def test_launch_cancel_active_kills_real_holder_via_cli(cli_env, capsys):
    code = (
        "import time\n"
        "from cdx_agent import config, runtime, session\n"
        f"cfg = config.load_config()\n"
        f"repo = config.repo_root({str(cli_env['repo'])!r})\n"
        "rctx = runtime.runtime_context(cfg, repo, access_mode='safe', engine='codex')\n"
        "h = session.acquire_lock(session.lock_path_for(rctx.runtime_dir))\n"
        "time.sleep(30)\n"
    )
    env = dict(os.environ)
    env["HOME"] = str(cli_env["home"])
    holder = subprocess.Popen([sys.executable, "-c", code], env=env)
    try:
        deadline = time.time() + 5
        lock_path = None
        while time.time() < deadline:
            from cdx_agent import config as config_mod
            from cdx_agent import runtime as runtime_mod
            from cdx_agent import session as session_mod

            cfg = config_mod.load_config()
            repo = config_mod.repo_root(cli_env["repo"])
            rctx = runtime_mod.runtime_context(cfg, repo, access_mode="safe", engine="codex")
            lock_path = session_mod.lock_path_for(rctx.runtime_dir)
            if session_mod.read_lock_pid(lock_path) == holder.pid and session_mod.is_lock_held_by(
                lock_path, holder.pid
            ):
                break
            time.sleep(0.05)
        else:
            pytest.fail("holder never acquired the lock in time")

        exit_code = main(["cancel-active", "--repo", str(cli_env["repo"]), "--engine", "codex", "--safe"])
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "action=killed" in out
        holder.wait(timeout=5)
        assert holder.returncode is not None
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=5)


def test_session_doctor_via_cli(cli_env, capsys):
    exit_code = main(["session-doctor", "--repo", str(cli_env["repo"]), "--engine", "codex", "--safe"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "runtime_dir=" in out
    assert "lock_pid=None" in out


def test_skills_list_and_audit_via_cli(cli_env, capsys):
    from cdx_agent import config as config_mod

    cfg = config_mod.load_config()
    skill_dir = cfg.tools_root / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: A perfectly safe demo skill used for CLI testing.\n---\n\nDo safe things.\n"
    )

    exit_code = main(["skills-list"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "demo ::" in out

    exit_code = main(["skills-audit"])
    assert exit_code == 0


def test_install_hooks_via_cli(cli_env, capsys):
    exit_code = main(["install-hooks", "--repo", str(cli_env["repo"]), "--engine", "codex"])
    assert exit_code == 0
    assert (cli_env["repo"] / ".codex" / "hooks.json").is_file()


def test_sync_docs_via_cli(cli_env, capsys):
    exit_code = main(["sync-docs", "--repo", str(cli_env["repo"]), "--engine", "claude"])
    assert exit_code == 0
    assert (cli_env["repo"] / "CLAUDE.md").is_file()


def test_init_user_via_cli(cli_env, capsys):
    exit_code = main(["init-user"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "config_path=" in out
    assert "seeded_files=" in out


def test_reap_stale_runtimes_via_cli_reports_nothing_on_clean_state(cli_env, capsys):
    exit_code = main(["reap-stale-runtimes"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "No stale runtime directories found." in out


def test_resync_via_cli(cli_env, capsys):
    exit_code = main(["resync", "--repo", str(cli_env["repo"]), "--engine", "codex", "--safe"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "config\t" in out
    assert "auth\t" in out


def test_dg_workspace_roundtrip_via_cli(cli_env, capsys):
    dep1 = cli_env["tmp_path"] / "dep1"
    dep2 = cli_env["tmp_path"] / "dep2"
    dep1.mkdir()
    dep2.mkdir()

    assert main(["dg-workspace-init", "--name", "myws", str(dep1), str(dep2)]) == 0
    capsys.readouterr()

    assert main(["dg-workspace-list"]) == 0
    out = capsys.readouterr().out
    assert "myws ->" in out

    assert main(["dg-workspace-show", "--name", "myws"]) == 0
    out = capsys.readouterr().out
    assert "present" in out

    assert main(["dg-workspace-clean", "--name", "myws"]) == 0
    out = capsys.readouterr().out
    assert "No generated mirror to remove" in out


def test_dg_reports_missing_binary_gracefully_via_cli(cli_env, capsys, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    exit_code = main(["dg", "--root", str(cli_env["repo"])])
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "dg not found on PATH" in err
