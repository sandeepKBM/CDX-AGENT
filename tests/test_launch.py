import os
import stat
from pathlib import Path

from cdx_agent import config as config_mod
from cdx_agent import launch


def _cfg(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".codex").mkdir()
    (home / ".codex" / "config.toml").write_text('model = "gpt-5.4-mini"\nmodel_reasoning_effort = "xhigh"\n')
    (home / ".codex" / "auth.json").write_text('{"token": "fake"}')
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text("{}")
    cfg = config_mod.Config.defaults(home=home)
    (cfg.tools_root / "hooks").mkdir(parents=True)
    return cfg


def _repo(tmp_path, name="myrepo"):
    repo = tmp_path / name
    repo.mkdir()
    return repo


def _write_stub_binary(bin_dir: Path, name: str, marker_dir: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / name
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys, json\n"
        f"marker = {str(marker_dir / (name + '_invocation.json'))!r}\n"
        "payload = {'argv': sys.argv[1:], 'cwd': os.getcwd(), 'env_marker': os.environ.get('CODEX_HOME', '')}\n"
        "open(marker, 'w').write(json.dumps(payload))\n"
        "sys.exit(0)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


# --- command building -----------------------------------------------------------------


def test_build_codex_command_maps_full_to_danger_full_access(tmp_path):
    cmd = launch.build_codex_command(tmp_path, "full", [])
    assert "--sandbox" in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "danger-full-access"


def test_build_codex_command_maps_safe_to_workspace_write(tmp_path):
    cmd = launch.build_codex_command(tmp_path, "safe", [])
    assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"


def test_build_claude_command_maps_full_to_bypass_permissions(tmp_path):
    cmd = launch.build_claude_command(tmp_path, "full", [])
    assert cmd[cmd.index("--permission-mode") + 1] == "bypassPermissions"


def test_build_claude_command_maps_safe_to_default(tmp_path):
    cmd = launch.build_claude_command(tmp_path, "safe", [])
    assert cmd[cmd.index("--permission-mode") + 1] == "default"


def test_build_command_dispatches_by_engine(tmp_path):
    codex_cmd = launch.build_command("codex", tmp_path, "safe")
    claude_cmd = launch.build_command("claude", tmp_path, "safe")
    assert codex_cmd[0] == "codex"
    assert claude_cmd[0] == "claude"


# --- prepare_launch --------------------------------------------------------------------


def test_prepare_launch_dry_run_does_not_create_runtime_dir(tmp_path):
    cfg = _cfg(tmp_path)
    repo = _repo(tmp_path)
    result = launch.prepare_launch(cfg, repo, engine="codex", access_mode="safe", dry_run=True)
    assert result.plan is not None
    assert not result.plan.runtime.runtime_dir.exists()
    assert result.lock_acquired is False


def test_prepare_launch_links_skills_and_installs_hooks(tmp_path):
    cfg = _cfg(tmp_path)
    repo = _repo(tmp_path)
    skill_dir = cfg.tools_root / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: A perfectly safe demo skill for tests.\n---\n\nDo safe things.\n"
    )

    result = launch.prepare_launch(cfg, repo, engine="codex", access_mode="safe")
    assert result.lock_acquired is True
    assert result.plan is not None
    assert any(d.skill_name == "demo-skill" and d.action == "linked" for d in result.link_decisions)
    assert result.plan.runtime.skills_dir.joinpath("demo-skill").is_symlink()
    assert result.hook_install.hooks_json_path.is_file()
    assert result.doc_sync.action == "created"
    if result.lock_handle is not None:
        from cdx_agent import session

        session.release_lock(result.lock_handle)


def test_prepare_launch_codex_and_claude_use_isolated_runtimes(tmp_path):
    cfg = _cfg(tmp_path)
    repo = _repo(tmp_path)
    codex_result = launch.prepare_launch(cfg, repo, engine="codex", access_mode="safe")
    claude_result = launch.prepare_launch(cfg, repo, engine="claude", access_mode="safe")
    assert codex_result.plan.runtime.runtime_dir != claude_result.plan.runtime.runtime_dir

    from cdx_agent import session

    for result in (codex_result, claude_result):
        if result.lock_handle is not None:
            session.release_lock(result.lock_handle)


def test_prepare_launch_diagnoses_only_when_lock_held(tmp_path):
    cfg = _cfg(tmp_path)
    repo = _repo(tmp_path)
    first = launch.prepare_launch(cfg, repo, engine="codex", access_mode="safe")
    assert first.lock_acquired is True

    second = launch.prepare_launch(cfg, repo, engine="codex", access_mode="safe")
    assert second.lock_acquired is False
    assert second.plan is None
    assert second.diagnosis is not None
    assert second.diagnosis.lock_pid == os.getpid()

    from cdx_agent import session

    session.release_lock(first.lock_handle)


# --- secondary mode: join a live session instead of fighting the lock or killing it -----


def test_prepare_launch_secondary_does_not_take_lock_or_conflict(tmp_path):
    cfg = _cfg(tmp_path)
    repo = _repo(tmp_path)
    primary = launch.prepare_launch(cfg, repo, engine="codex", access_mode="safe")
    assert primary.lock_acquired is True

    secondary = launch.prepare_launch(cfg, repo, engine="codex", access_mode="safe", secondary=True)
    assert secondary.plan is not None
    assert secondary.lock_acquired is False
    assert secondary.lock_handle is None

    from cdx_agent import session

    # the primary session's lock must be completely untouched
    assert session.read_lock_pid(session.lock_path_for(primary.plan.runtime.runtime_dir)) == os.getpid()
    session.release_lock(primary.lock_handle)


def test_prepare_launch_secondary_skips_skills_hooks_doc_resync(tmp_path):
    cfg = _cfg(tmp_path)
    repo = _repo(tmp_path)
    primary = launch.prepare_launch(cfg, repo, engine="codex", access_mode="safe")

    secondary = launch.prepare_launch(cfg, repo, engine="codex", access_mode="safe", secondary=True)
    assert secondary.link_decisions == ()
    assert secondary.doc_sync is None
    assert secondary.hook_install is None

    from cdx_agent import session

    session.release_lock(primary.lock_handle)


def test_launch_secondary_runs_alongside_a_live_primary_without_killing_it(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    repo = _repo(tmp_path)
    marker_dir = tmp_path / "markers"
    marker_dir.mkdir()
    stub_bin = tmp_path / "stub_bin"
    _write_stub_binary(stub_bin, "codex", marker_dir)
    monkeypatch.setenv("PATH", f"{stub_bin}{os.pathsep}{os.environ['PATH']}")

    from cdx_agent import runtime as runtime_mod
    from cdx_agent import session

    rctx = runtime_mod.runtime_context(cfg, repo, access_mode="safe", engine="codex")
    primary_handle = session.acquire_lock(session.lock_path_for(rctx.runtime_dir))
    try:
        outcome = launch.launch(cfg, repo, engine="codex", access_mode="safe", secondary=True)
        assert outcome.exit_code == 0
        # the primary's lock must still be held by us afterward -- secondary
        # launches must never release or contend for it
        assert session.read_lock_pid(session.lock_path_for(rctx.runtime_dir)) == primary_handle.pid
    finally:
        session.release_lock(primary_handle)


# --- launch() end-to-end with stub binaries ---------------------------------------------


def test_launch_dry_run_returns_zero_without_invoking_binary(tmp_path):
    cfg = _cfg(tmp_path)
    repo = _repo(tmp_path)
    outcome = launch.launch(cfg, repo, engine="codex", access_mode="safe", dry_run=True)
    assert outcome.exit_code == 0


def test_launch_invokes_stub_codex_binary_with_expected_env_and_cwd(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    repo = _repo(tmp_path)
    marker_dir = tmp_path / "markers"
    marker_dir.mkdir()
    stub_bin = tmp_path / "stub_bin"
    _write_stub_binary(stub_bin, "codex", marker_dir)
    monkeypatch.setenv("PATH", f"{stub_bin}{os.pathsep}{os.environ['PATH']}")

    outcome = launch.launch(cfg, repo, engine="codex", access_mode="safe")
    assert outcome.exit_code == 0

    import json

    payload = json.loads((marker_dir / "codex_invocation.json").read_text())
    assert payload["cwd"] == str(repo.resolve())
    assert payload["env_marker"] == str(outcome.prepare.plan.runtime.runtime_dir)
    assert "--sandbox" in payload["argv"]


def test_launch_invokes_stub_claude_binary(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    repo = _repo(tmp_path)
    marker_dir = tmp_path / "markers"
    marker_dir.mkdir()
    stub_bin = tmp_path / "stub_bin"
    _write_stub_binary(stub_bin, "claude", marker_dir)
    monkeypatch.setenv("PATH", f"{stub_bin}{os.pathsep}{os.environ['PATH']}")

    outcome = launch.launch(cfg, repo, engine="claude", access_mode="full")
    assert outcome.exit_code == 0

    import json

    payload = json.loads((marker_dir / "claude_invocation.json").read_text())
    assert payload["cwd"] == str(repo.resolve())
    assert "--permission-mode" in payload["argv"]
    assert "bypassPermissions" in payload["argv"]


def test_launch_returns_none_exit_code_when_lock_conflict(tmp_path):
    cfg = _cfg(tmp_path)
    repo = _repo(tmp_path)
    from cdx_agent import runtime as runtime_mod
    from cdx_agent import session

    rctx = runtime_mod.runtime_context(cfg, repo, access_mode="safe", engine="codex")
    handle = session.acquire_lock(session.lock_path_for(rctx.runtime_dir))
    try:
        outcome = launch.launch(cfg, repo, engine="codex", access_mode="safe")
        assert outcome.exit_code is None
        assert outcome.prepare.plan is None
    finally:
        session.release_lock(handle)


# --- sync_docs_for_repo ------------------------------------------------------------------


def test_sync_docs_for_repo_writes_correct_filename_per_engine(tmp_path):
    cfg = _cfg(tmp_path)
    repo = _repo(tmp_path)
    codex_result = launch.sync_docs_for_repo(cfg, repo, engine="codex")
    assert codex_result.path.name == "AGENTS.md"
    assert codex_result.path.is_file()

    claude_result = launch.sync_docs_for_repo(cfg, repo, engine="claude")
    assert claude_result.path.name == "CLAUDE.md"
    assert claude_result.path.is_file()
