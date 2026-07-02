import os
import time
from pathlib import Path

import pytest

from cdx_agent import config as config_mod
from cdx_agent import runtime as runtime_mod


def _make_config(tmp_path: Path) -> config_mod.Config:
    home = tmp_path / "home"
    home.mkdir()
    account = home / ".codex"
    account.mkdir(parents=True)
    (account / "config.toml").write_text('model = "gpt-5.4-mini"\nmodel_reasoning_effort = "xhigh"\n')
    (account / "auth.json").write_text('{"token": "fake-token"}')
    return config_mod.Config.defaults(home=home)


def _make_repo(tmp_path: Path, name: str = "myrepo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    return repo


def test_full_and_safe_get_isolated_runtime_dirs(tmp_path):
    # A1: the bash predecessor's current_codex_home() had both the "full" and
    # "safe" branches call the exact same function, so a --safe launch shared
    # config.toml/auth.json/lock/skills with the default --full launch for the
    # same repo. Here they must resolve to different directories.
    cfg = _make_config(tmp_path)
    repo = _make_repo(tmp_path)

    full_dir = runtime_mod.runtime_home(cfg, repo, access_mode="full", engine="codex")
    safe_dir = runtime_mod.runtime_home(cfg, repo, access_mode="safe", engine="codex")
    assert full_dir != safe_dir
    assert "full" in full_dir.parts
    assert "safe" in safe_dir.parts


def test_codex_and_claude_engines_get_isolated_runtime_dirs(tmp_path):
    cfg = _make_config(tmp_path)
    repo = _make_repo(tmp_path)

    codex_dir = runtime_mod.runtime_home(cfg, repo, access_mode="safe", engine="codex")
    claude_dir = runtime_mod.runtime_home(cfg, repo, access_mode="safe", engine="claude")
    assert codex_dir != claude_dir


def test_provisioning_one_access_mode_does_not_touch_the_other(tmp_path):
    cfg = _make_config(tmp_path)
    repo = _make_repo(tmp_path)

    full_ctx = runtime_mod.provision_runtime(cfg, repo, access_mode="full", engine="codex")
    assert full_ctx.config_path.is_file()

    safe_dir = runtime_mod.runtime_home(cfg, repo, access_mode="safe", engine="codex")
    assert not safe_dir.exists()


def test_migrate_legacy_runtime_copies_into_full_slot(tmp_path):
    cfg = _make_config(tmp_path)
    repo = _make_repo(tmp_path)

    legacy = runtime_mod.legacy_runtime_home(cfg, repo)
    legacy.mkdir(parents=True)
    (legacy / "config.toml").write_text("model = \"legacy\"\n")
    (legacy / "auth.json").write_text('{"token": "legacy-token"}')

    migrated = runtime_mod.migrate_legacy_runtime(cfg, repo)
    assert migrated == runtime_mod.runtime_home(cfg, repo, access_mode="full", engine="codex")
    assert (migrated / "config.toml").read_text() == "model = \"legacy\"\n"

    safe_dir = runtime_mod.runtime_home(cfg, repo, access_mode="safe", engine="codex")
    assert not safe_dir.exists()


def test_migrate_legacy_runtime_is_noop_when_nothing_to_migrate(tmp_path):
    cfg = _make_config(tmp_path)
    repo = _make_repo(tmp_path)
    assert runtime_mod.migrate_legacy_runtime(cfg, repo) is None


def test_config_resyncs_when_source_changes(tmp_path):
    # A7: bash's ensure_runtime_config/copy_auth_if_needed only wrote a file
    # if absent, so editing ~/.codex/config.toml (or re-logging in) never
    # propagated to an already-provisioned runtime. Sync must pick up changes.
    cfg = _make_config(tmp_path)
    repo = _make_repo(tmp_path)
    rctx = runtime_mod.runtime_context(cfg, repo, access_mode="safe", engine="codex")

    first = runtime_mod.sync_runtime_config(cfg, rctx)
    assert first.action == "created"
    assert 'model = "gpt-5.4-mini"' in rctx.config_path.read_text()

    second = runtime_mod.sync_runtime_config(cfg, rctx)
    assert second.action == "unchanged"

    (cfg.account_home / ".codex" / "config.toml").write_text(
        'model = "gpt-6.0"\nmodel_reasoning_effort = "xhigh"\n'
    )
    third = runtime_mod.sync_runtime_config(cfg, rctx)
    assert third.action == "updated"
    assert 'model = "gpt-6.0"' in rctx.config_path.read_text()


def test_auth_resyncs_after_relogin(tmp_path):
    cfg = _make_config(tmp_path)
    repo = _make_repo(tmp_path)
    rctx = runtime_mod.runtime_context(cfg, repo, access_mode="safe", engine="codex")

    runtime_mod.sync_runtime_auth(cfg, rctx)
    assert rctx.auth_path.read_text() == '{"token": "fake-token"}'

    (cfg.account_home / ".codex" / "auth.json").write_text('{"token": "refreshed-token"}')
    result = runtime_mod.sync_runtime_auth(cfg, rctx)
    assert result.action == "updated"
    assert rctx.auth_path.read_text() == '{"token": "refreshed-token"}'


def test_resync_does_not_clobber_user_edited_runtime_config_without_warning(tmp_path):
    cfg = _make_config(tmp_path)
    repo = _make_repo(tmp_path)
    rctx = runtime_mod.runtime_context(cfg, repo, access_mode="safe", engine="codex")

    runtime_mod.sync_runtime_config(cfg, rctx)
    # user hand-edits the runtime copy directly
    rctx.config_path.write_text("model = \"hand-edited\"\n")

    # source also changes -> real conflict, must not silently overwrite
    (cfg.account_home / ".codex" / "config.toml").write_text(
        'model = "gpt-7.0"\nmodel_reasoning_effort = "xhigh"\n'
    )
    result = runtime_mod.sync_runtime_config(cfg, rctx)
    assert result.action == "conflict"
    assert rctx.config_path.read_text() == "model = \"hand-edited\"\n"

    # explicit resync forces the pull regardless
    results = runtime_mod.resync(cfg, rctx)
    config_result = next(r for r in results if r.key == "config")
    assert config_result.action == "updated"
    assert 'model = "gpt-7.0"' in rctx.config_path.read_text()


def test_reap_stale_runtimes_respects_age_and_containment(tmp_path):
    cfg = _make_config(tmp_path)
    cfg.runtime_root.mkdir(parents=True, exist_ok=True)

    young_stale = cfg.runtime_root / "host" / "codex" / "full" / "repo__abc.stale.20260101_000000"
    old_stale = cfg.runtime_root / "host" / "codex" / "full" / "repo__def.stale.20250101_000000"
    young_stale.mkdir(parents=True)
    old_stale.mkdir(parents=True)
    (young_stale / "auth.json").write_text('{"token": "young"}')
    (old_stale / "auth.json").write_text('{"token": "old"}')

    old_time = time.time() - (60 * 86400)
    os.utime(old_stale, (old_time, old_time))

    reports = runtime_mod.reap_stale_runtimes(cfg, max_age_days=14, dry_run=True)
    actions = {r.path: r.action for r in reports}
    assert actions[young_stale] == "kept"
    assert actions[old_stale] == "pending"
    assert old_stale.exists()  # dry-run never deletes

    live_reports = runtime_mod.reap_stale_runtimes(cfg, max_age_days=14, dry_run=False)
    live_actions = {r.path: r.action for r in live_reports}
    assert live_actions[old_stale] == "reaped"
    assert not old_stale.exists()
    assert young_stale.exists()


def test_reap_stale_runtimes_refuses_to_delete_outside_runtime_root(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    outside = tmp_path / "not_runtime_root" / "innocent.stale.x"
    outside.mkdir(parents=True)

    with pytest.raises(ValueError):
        runtime_mod._safe_remove_runtime_tree(cfg, outside)
    assert outside.exists()
