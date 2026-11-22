import json
import os
from pathlib import Path

import pytest

from cdx_agent import config as config_mod
from cdx_agent import hooks


def _cfg(tmp_path):
    cfg = config_mod.Config.defaults(home=tmp_path / "home")
    hooks_root = cfg.tools_root / "hooks"
    hooks_root.mkdir(parents=True)
    for name in hooks.HOOK_SCRIPTS:
        (hooks_root / name).write_text("#!/usr/bin/env python3\n")
    return cfg


def test_build_hooks_payload_wires_all_four_scripts(tmp_path):
    # A6/D3 fix: the bash predecessor's generated hooks.json only referenced
    # 3 of the 4 installed scripts (silently dropped token_risk_warn.py).
    payload = hooks.build_hooks_payload(tmp_path / "hooks")
    assert hooks.referenced_scripts(payload) == set(hooks.HOOK_SCRIPTS) - hooks.LINKED_ONLY


def test_generated_hooks_json_matches_example_template_script_set(tmp_path):
    example_path = os.environ.get(
        "CDX_AGENT_HOOKS_EXAMPLE_JSON",
        str(Path(__file__).resolve().parents[1] / "tools" / "hooks" / "hooks.example.json"),
    )
    example = Path(example_path)
    if not example.is_file():
        pytest.skip("hooks.example.json not present on this machine")
    example_payload = json.loads(example.read_text())
    payload = hooks.build_hooks_payload(tmp_path / "hooks")
    assert hooks.referenced_scripts(payload) == hooks.referenced_scripts(example_payload)


def test_install_hook_scripts_symlinks_all_present_scripts(tmp_path):
    cfg = _cfg(tmp_path)
    dst = tmp_path / "dst" / "hooks"
    linked = hooks.install_hook_scripts(cfg.tools_root / "hooks", dst)
    assert {p.name for p in linked} == set(hooks.HOOK_SCRIPTS)
    for name in hooks.HOOK_SCRIPTS:
        assert (dst / name).is_symlink()


def test_install_hook_scripts_backs_up_and_replaces_stale_target(tmp_path):
    cfg = _cfg(tmp_path)
    dst = tmp_path / "dst" / "hooks"
    dst.mkdir(parents=True)
    stale = dst / "pre_tool_use_policy.py"
    stale.write_text("old content")

    hooks.install_hook_scripts(cfg.tools_root / "hooks", dst)
    assert (dst / "pre_tool_use_policy.py").is_symlink()
    backups = list(dst.glob("pre_tool_use_policy.py.bak.*"))
    assert len(backups) == 1
    assert backups[0].read_text() == "old content"


def test_write_hooks_json_backs_up_existing(tmp_path):
    hooks_dir = tmp_path / "hooks"
    hooks_json = tmp_path / "hooks.json"
    hooks_json.write_text("{}")
    hooks.write_hooks_json(hooks_dir, hooks_json)
    assert json.loads(hooks_json.read_text())
    backups = list(tmp_path.glob("hooks.json.bak.*"))
    assert len(backups) == 1


def test_hooks_locations_for_repo_differ_by_engine(tmp_path):
    repo = tmp_path / "repo"
    codex_dir, codex_json = hooks.hooks_locations_for_repo(repo, "codex")
    claude_dir, claude_json = hooks.hooks_locations_for_repo(repo, "claude")
    assert codex_dir != claude_dir
    assert codex_json != claude_json
    assert codex_dir == repo / ".codex" / "hooks"
    assert claude_dir == repo / ".claude" / "hooks"


def test_install_hooks_for_repo_end_to_end(tmp_path):
    cfg = _cfg(tmp_path)
    repo = tmp_path / "myrepo"
    repo.mkdir()
    result = hooks.install_hooks_for_repo(cfg, repo, engine="codex")
    assert result.hooks_json_path == repo / ".codex" / "hooks.json"
    assert result.hooks_json_path.is_file()
    payload = json.loads(result.hooks_json_path.read_text())
    assert hooks.referenced_scripts(payload) == set(hooks.HOOK_SCRIPTS) - hooks.LINKED_ONLY
    assert len(result.linked_scripts) == len(hooks.HOOK_SCRIPTS)


def test_install_hooks_for_runtime_both_engines_isolated(tmp_path):
    cfg = _cfg(tmp_path)
    codex_runtime = tmp_path / "runtime" / "codex" / "full" / "repo__abc"
    claude_runtime = tmp_path / "runtime" / "claude" / "full" / "repo__abc"
    codex_result = hooks.install_hooks_for_runtime(cfg, codex_runtime, engine="codex")
    claude_result = hooks.install_hooks_for_runtime(cfg, claude_runtime, engine="claude")
    assert codex_result.hooks_json_path != claude_result.hooks_json_path
    assert codex_result.hooks_json_path.is_file()
    assert claude_result.hooks_json_path.is_file()


def test_install_hooks_for_runtime_claude_writes_settings_file(tmp_path):
    cfg = _cfg(tmp_path)
    claude_runtime = tmp_path / "runtime" / "claude" / "full" / "repo__abc"
    result = hooks.install_hooks_for_runtime(cfg, claude_runtime, engine="claude")
    assert result.claude_settings_path is not None
    assert result.claude_settings_path.is_file()
    payload = json.loads(result.claude_settings_path.read_text())
    assert "hooks" in payload
    assert hooks.referenced_scripts(payload["hooks"]) == set(hooks.HOOK_SCRIPTS) - hooks.LINKED_ONLY


def test_install_hooks_for_runtime_codex_has_no_settings_file(tmp_path):
    cfg = _cfg(tmp_path)
    codex_runtime = tmp_path / "runtime" / "codex" / "full" / "repo__abc"
    result = hooks.install_hooks_for_runtime(cfg, codex_runtime, engine="codex")
    assert result.claude_settings_path is None


def test_build_claude_settings_payload_wraps_hooks_under_hooks_key(tmp_path):
    payload = hooks.build_claude_settings_payload(tmp_path / "hooks")
    assert set(payload.keys()) == {"hooks"}
    assert hooks.referenced_scripts(payload["hooks"]) == set(hooks.HOOK_SCRIPTS) - hooks.LINKED_ONLY


def test_build_claude_settings_payload_merges_base_settings(tmp_path):
    base = tmp_path / "claude_settings.json"
    base.write_text(json.dumps({"theme": "dark", "model": "sonnet"}))
    payload = hooks.build_claude_settings_payload(tmp_path / "hooks", base_settings_path=base)
    assert payload["theme"] == "dark"
    assert payload["model"] == "sonnet"
    assert "hooks" in payload


def test_build_claude_settings_payload_tolerates_missing_or_corrupt_base(tmp_path):
    missing = tmp_path / "does-not-exist.json"
    payload = hooks.build_claude_settings_payload(tmp_path / "hooks", base_settings_path=missing)
    assert "hooks" in payload

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("not valid json{{{")
    payload2 = hooks.build_claude_settings_payload(tmp_path / "hooks", base_settings_path=corrupt)
    assert "hooks" in payload2


def test_write_claude_settings_backs_up_existing(tmp_path):
    output = tmp_path / "claude_hooks_settings.json"
    output.write_text("{}")
    hooks.write_claude_settings(tmp_path / "hooks", None, output)
    assert json.loads(output.read_text())["hooks"]
    backups = list(tmp_path.glob("claude_hooks_settings.json.bak.*"))
    assert len(backups) == 1


def test_claude_settings_path_for_runtime_differs_from_config_path(tmp_path):
    # Must not collide with RuntimeContext.config_path's "claude_settings.json"
    # (the synced copy of the user's real ~/.claude/settings.json).
    path = hooks.claude_settings_path_for_runtime(tmp_path)
    assert path.name != "claude_settings.json"
    assert path == tmp_path / "claude_hooks_settings.json"


def test_install_hook_scripts_relink_is_idempotent_no_bak_accumulation(tmp_path):
    # Regression: every launch used to backup-and-relink even when the target
    # already pointed at the right source, growing one .bak per launch forever.
    cfg = _cfg(tmp_path)
    dst = tmp_path / "dst" / "hooks"
    hooks.install_hook_scripts(cfg.tools_root / "hooks", dst)
    hooks.install_hook_scripts(cfg.tools_root / "hooks", dst)
    hooks.install_hook_scripts(cfg.tools_root / "hooks", dst)
    assert list(dst.glob("*.bak.*")) == []
    for name in hooks.HOOK_SCRIPTS:
        assert (dst / name).is_symlink()


def test_install_hook_scripts_still_replaces_wrong_symlink(tmp_path):
    cfg = _cfg(tmp_path)
    dst = tmp_path / "dst" / "hooks"
    dst.mkdir(parents=True)
    wrong_src = tmp_path / "elsewhere.py"
    wrong_src.write_text("wrong\n")
    (dst / "token_risk_warn.py").symlink_to(wrong_src)

    hooks.install_hook_scripts(cfg.tools_root / "hooks", dst)
    target = dst / "token_risk_warn.py"
    assert target.readlink() == cfg.tools_root / "hooks" / "token_risk_warn.py"
    assert len(list(dst.glob("token_risk_warn.py.bak.*"))) == 1


def test_write_hooks_json_unchanged_content_no_bak(tmp_path):
    hooks_dir = tmp_path / "hooks"
    hooks_json = tmp_path / "hooks.json"
    hooks.write_hooks_json(hooks_dir, hooks_json)
    hooks.write_hooks_json(hooks_dir, hooks_json)
    hooks.write_hooks_json(hooks_dir, hooks_json)
    assert list(tmp_path.glob("hooks.json.bak.*")) == []


def test_write_claude_settings_unchanged_content_no_bak(tmp_path):
    output = tmp_path / "claude_hooks_settings.json"
    hooks.write_claude_settings(tmp_path / "hooks", None, output)
    hooks.write_claude_settings(tmp_path / "hooks", None, output)
    assert list(tmp_path.glob("claude_hooks_settings.json.bak.*")) == []


def test_claude_settings_env_defaults_user_env_wins(tmp_path):
    env_file = tmp_path / "claude_env.json"
    env_file.write_text(json.dumps({"_comment": "x", "CLAUDE_CODE_SUBAGENT_MODEL": "sonnet", "A": "1"}))
    base = tmp_path / "base.json"
    base.write_text(json.dumps({"env": {"A": "user"}}))
    payload = hooks.build_claude_settings_payload(tmp_path / "hooks", base, env_file)
    assert payload["env"] == {"CLAUDE_CODE_SUBAGENT_MODEL": "sonnet", "A": "user"}
    assert "env" not in hooks.build_claude_settings_payload(tmp_path / "hooks", None, None)


def test_install_agent_defs_never_overwrites_user_files(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir(); dst.mkdir()
    (src / "Explore.md").write_text("---\nmodel: haiku\n# managed-by: cdx-agent\n---\nv2\n")
    (src / "mine.md").write_text("---\n# managed-by: cdx-agent\n---\nours\n")
    (dst / "mine.md").write_text("user's own agent\n")
    (dst / "Explore.md").write_text("---\n# managed-by: cdx-agent\n---\nv1\n")
    out = hooks.install_agent_defs(src, dst)
    assert [p.name for p in out] == ["Explore.md"]
    assert (dst / "Explore.md").read_text().endswith("v2\n") and (dst / "mine.md").read_text() == "user's own agent\n"
    assert hooks.install_agent_defs(src, dst) == []


def test_safe_sandbox_block_only_when_enabled(tmp_path, monkeypatch):
    import json as _json
    from cdx_agent import hooks as _h
    f = tmp_path / "sb.json"
    f.write_text(_json.dumps({"_c": "x", "enabled": False, "filesystem": {"denyWrite": ["/p"]}}))
    monkeypatch.delenv("CDX_SAFE_SANDBOX", raising=False)
    assert "sandbox" not in _h.build_claude_settings_payload(tmp_path, None, None, f)
    monkeypatch.setenv("CDX_SAFE_SANDBOX", "1")
    sb = _h.build_claude_settings_payload(tmp_path, None, None, f)["sandbox"]
    assert sb["enabled"] is True and sb["filesystem"]["denyWrite"] == ["/p"] and "_c" not in sb
    assert "sandbox" not in _h.build_claude_settings_payload(tmp_path, None, None, None)


def test_pretooluse_matcher_skips_edit_write(tmp_path):
    from cdx_agent import hooks as _h
    pre = _h.build_hooks_payload(tmp_path)["PreToolUse"]
    assert pre[0]["matcher"] == "Bash|apply_patch"
    assert not any("token_risk_warn" in h["command"] for e in pre for h in e["hooks"])
