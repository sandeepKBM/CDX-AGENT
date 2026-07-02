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
    assert hooks.referenced_scripts(payload) == set(hooks.HOOK_SCRIPTS)


def test_generated_hooks_json_matches_example_template_script_set(tmp_path):
    example_path = os.environ.get(
        "CDX_AGENT_HOOKS_EXAMPLE_JSON",
        "/common/users/ss5772/codex_tools/hooks/hooks.example.json",
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
    assert hooks.referenced_scripts(payload) == set(hooks.HOOK_SCRIPTS)
    assert len(result.linked_scripts) == 4


def test_install_hooks_for_runtime_both_engines_isolated(tmp_path):
    cfg = _cfg(tmp_path)
    codex_runtime = tmp_path / "runtime" / "codex" / "full" / "repo__abc"
    claude_runtime = tmp_path / "runtime" / "claude" / "full" / "repo__abc"
    codex_result = hooks.install_hooks_for_runtime(cfg, codex_runtime, engine="codex")
    claude_result = hooks.install_hooks_for_runtime(cfg, claude_runtime, engine="claude")
    assert codex_result.hooks_json_path != claude_result.hooks_json_path
    assert codex_result.hooks_json_path.is_file()
    assert claude_result.hooks_json_path.is_file()
