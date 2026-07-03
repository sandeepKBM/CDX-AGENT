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


def test_runtime_context_claude_skills_dir_is_dot_claude_skills(tmp_path):
    # Claude Code only discovers skills from <added-dir>/.claude/skills/
    # (empirically verified against a live claude invocation), unlike Codex
    # which reads $CODEX_HOME/skills because CODEX_HOME redirects its whole
    # config home. The two engines need different skills_dir layouts.
    cfg = _make_config(tmp_path)
    repo = _make_repo(tmp_path)

    codex_ctx = runtime_mod.runtime_context(cfg, repo, access_mode="safe", engine="codex")
    claude_ctx = runtime_mod.runtime_context(cfg, repo, access_mode="safe", engine="claude")

    assert codex_ctx.skills_dir == codex_ctx.runtime_dir / "skills"
    assert claude_ctx.skills_dir == claude_ctx.runtime_dir / ".claude" / "skills"


def test_sync_runtime_auth_skips_for_claude(tmp_path):
    # Claude Code manages its own auth; there's nothing to sync, and syncing
    # a copied credentials-shaped file would sit in a directory granted
    # broad tool-read access via --add-dir for no benefit.
    cfg = _make_config(tmp_path)
    repo = _make_repo(tmp_path)
    rctx = runtime_mod.runtime_context(cfg, repo, access_mode="safe", engine="claude")

    result = runtime_mod.sync_runtime_auth(cfg, rctx)
    assert result.action == "skipped"
    assert not rctx.auth_path.exists()


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


def test_provision_claude_prunes_legacy_top_level_skills_dir(tmp_path):
    # Pre-engine-split claude runtimes linked skills into a top-level skills/
    # (the codex layout); Claude Code only discovers .claude/skills/ via
    # --add-dir, so the orphan just feeds stale symlinks forever unless pruned.
    cfg = _make_config(tmp_path)
    repo = _make_repo(tmp_path)
    rctx = runtime_mod.runtime_context(cfg, repo, access_mode="safe", engine="claude")
    legacy = rctx.runtime_dir / "skills"
    legacy.mkdir(parents=True)
    real_skill = tmp_path / "some-skill"
    real_skill.mkdir()
    (legacy / "some-skill").symlink_to(real_skill)

    runtime_mod.provision_runtime(cfg, repo, access_mode="safe", engine="claude")
    assert not legacy.exists()
    assert real_skill.is_dir()  # only the symlink container goes, never the target


def test_provision_codex_keeps_top_level_skills_dir(tmp_path):
    # For codex the top-level skills/ IS the active layout (CODEX_HOME/skills).
    cfg = _make_config(tmp_path)
    repo = _make_repo(tmp_path)
    rctx = runtime_mod.runtime_context(cfg, repo, access_mode="safe", engine="codex")
    rctx.skills_dir.mkdir(parents=True)

    runtime_mod.provision_runtime(cfg, repo, access_mode="safe", engine="codex")
    assert rctx.skills_dir.is_dir()


def test_provision_claude_dry_run_does_not_prune(tmp_path):
    cfg = _make_config(tmp_path)
    repo = _make_repo(tmp_path)
    rctx = runtime_mod.runtime_context(cfg, repo, access_mode="safe", engine="claude")
    legacy = rctx.runtime_dir / "skills"
    legacy.mkdir(parents=True)

    runtime_mod.provision_runtime(cfg, repo, access_mode="safe", engine="claude", dry_run=True)
    assert legacy.is_dir()


def test_reap_dry_run_mutates_nothing_including_permissions(tmp_path):
    # Regression: report-only mode used to chmod credential files inside
    # stale runtimes -- a filesystem mutation from a documented read-only op.
    cfg = _make_config(tmp_path)
    stale = cfg.runtime_root / "host" / "codex" / "full" / "repo__x.stale.20260101"
    stale.mkdir(parents=True)
    cred = stale / "auth.json"
    cred.write_text("{}")
    cred.chmod(0o644)

    runtime_mod.reap_stale_runtimes(cfg, max_age_days=99999, dry_run=True)
    assert (cred.stat().st_mode & 0o777) == 0o644  # untouched in dry-run

    runtime_mod.reap_stale_runtimes(cfg, max_age_days=99999, dry_run=False)
    assert (cred.stat().st_mode & 0o777) == 0o600  # hardened only in --apply


def test_extract_toml_key_handles_spacing_variants(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('model="gpt-6.0"\nmodel_reasoning_effort  =  "high"\n')
    assert runtime_mod._extract_toml_key(path, "model") == 'model = "gpt-6.0"'
    assert runtime_mod._extract_toml_key(path, "model_reasoning_effort") == 'model_reasoning_effort = "high"'


def test_extract_toml_key_ignores_keys_inside_tables(tmp_path):
    # A `model = ...` inside [profiles.x] must NOT be promoted to top level.
    path = tmp_path / "config.toml"
    path.write_text('[profiles.fast]\nmodel = "table-model"\n')
    assert runtime_mod._extract_toml_key(path, "model") is None


def test_extract_toml_key_top_level_wins_over_table(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('model = "top"\n[profiles.fast]\nmodel = "nested"\n')
    assert runtime_mod._extract_toml_key(path, "model") == 'model = "top"'
