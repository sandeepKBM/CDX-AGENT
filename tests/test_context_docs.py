from cdx_agent import context_docs


TEMPLATE = f"""
{context_docs.GENERATED_MARKER}

Repo-specific guidance for `__REPO_NAME__`:

- Rule one.
- Rule two.
"""


def test_render_substitutes_repo_name_and_carries_marker():
    rendered = context_docs.render(TEMPLATE, "my-repo")
    assert "my-repo" in rendered
    assert context_docs.GENERATED_MARKER in rendered


def test_target_path_differs_by_engine(tmp_path):
    repo = tmp_path / "repo"
    assert context_docs.target_path(repo, "codex").name == "AGENTS.md"
    assert context_docs.target_path(repo, "claude").name == "CLAUDE.md"


def test_sync_creates_new_file(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    result = context_docs.sync_repo_docs(repo, TEMPLATE, engine="codex")
    assert result.action == "created"
    assert (repo / "AGENTS.md").is_file()
    assert "repo" in (repo / "AGENTS.md").read_text()


def test_sync_is_idempotent_when_unchanged(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    context_docs.sync_repo_docs(repo, TEMPLATE, engine="codex")
    second = context_docs.sync_repo_docs(repo, TEMPLATE, engine="codex")
    assert second.action == "unchanged"


def test_sync_updates_generated_file_when_template_changes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    context_docs.sync_repo_docs(repo, TEMPLATE, engine="codex")
    new_template = TEMPLATE + "\n- Rule three.\n"
    result = context_docs.sync_repo_docs(repo, new_template, engine="codex")
    assert result.action == "updated"
    assert "Rule three" in (repo / "AGENTS.md").read_text()
    backups = list(repo.glob("AGENTS.md.bak.*"))
    assert len(backups) == 1


def test_sync_refuses_hand_written_file_without_marker(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("# Hand-written notes\nDo not touch.\n")
    result = context_docs.sync_repo_docs(repo, TEMPLATE, engine="codex")
    assert result.action == "refused_hand_written"
    assert (repo / "AGENTS.md").read_text() == "# Hand-written notes\nDo not touch.\n"


def test_does_not_clobber_hand_written_claude_md(tmp_path):
    # Direct regression test for the DeepReach/deepreach/CLAUDE.md scenario:
    # a real, hand-authored CLAUDE.md must survive a sync attempt untouched
    # unless the caller explicitly opts into adopt/force.
    repo = tmp_path / "deepreach"
    repo.mkdir()
    hand_written = "# DeepReach\n\nCommands, architecture notes written by a human.\n"
    (repo / "CLAUDE.md").write_text(hand_written)

    result = context_docs.sync_repo_docs(repo, TEMPLATE, engine="claude")
    assert result.action == "refused_hand_written"
    assert (repo / "CLAUDE.md").read_text() == hand_written


def test_sync_adopt_appends_after_hand_written_content(tmp_path):
    repo = tmp_path / "deepreach"
    repo.mkdir()
    hand_written = "# DeepReach\n\nHand-written architecture notes.\n"
    (repo / "CLAUDE.md").write_text(hand_written)

    result = context_docs.sync_repo_docs(repo, TEMPLATE, engine="claude", adopt=True)
    assert result.action == "adopted"
    content = (repo / "CLAUDE.md").read_text()
    assert content.startswith(hand_written.rstrip())
    assert context_docs.GENERATED_MARKER in content
    backups = list(repo.glob("CLAUDE.md.bak.*"))
    assert len(backups) == 1


def test_sync_force_overwrites_hand_written_content(tmp_path):
    repo = tmp_path / "deepreach"
    repo.mkdir()
    (repo / "CLAUDE.md").write_text("hand written\n")

    result = context_docs.sync_repo_docs(repo, TEMPLATE, engine="claude", force=True)
    assert result.action == "updated"
    content = (repo / "CLAUDE.md").read_text()
    assert "hand written" not in content
    assert context_docs.GENERATED_MARKER in content


def test_is_generated_detects_marker(tmp_path):
    generated = tmp_path / "AGENTS.md"
    generated.write_text(f"{context_docs.GENERATED_MARKER}\nsome content\n")
    hand_written = tmp_path / "CLAUDE.md"
    hand_written.write_text("no marker here\n")
    assert context_docs.is_generated(generated) is True
    assert context_docs.is_generated(hand_written) is False
    assert context_docs.is_generated(tmp_path / "missing.md") is False


def test_load_repo_template_falls_back_to_default(tmp_path):
    from cdx_agent import config as config_mod

    cfg = config_mod.Config.defaults(home=tmp_path / "home")
    template = context_docs.load_repo_template(cfg)
    assert context_docs.GENERATED_MARKER in template


def test_load_repo_template_reads_from_tools_root_when_present(tmp_path):
    from cdx_agent import config as config_mod

    cfg = config_mod.Config.defaults(home=tmp_path / "home")
    template_path = cfg.tools_root / "templates" / "repo.AGENTS.md"
    template_path.parent.mkdir(parents=True)
    template_path.write_text("custom template for __REPO_NAME__\n")
    template = context_docs.load_repo_template(cfg)
    assert "custom template" in template


def test_load_working_rules_template_falls_back_to_default(tmp_path):
    from cdx_agent import config as config_mod

    cfg = config_mod.Config.defaults(home=tmp_path / "home")
    template = context_docs.load_working_rules_template(cfg)
    assert context_docs.TOKEN_SAVER_START_MARKER in template
    assert context_docs.TOKEN_SAVER_END_MARKER in template


def test_load_working_rules_template_reads_from_tools_root_base_when_present(tmp_path):
    from cdx_agent import config as config_mod

    cfg = config_mod.Config.defaults(home=tmp_path / "home")
    template_path = cfg.tools_root / "base" / "AGENTS.md"
    template_path.parent.mkdir(parents=True)
    template_path.write_text("custom working rules\n")
    template = context_docs.load_working_rules_template(cfg)
    assert "custom working rules" in template


def test_render_working_rules_strips_token_saver_block_by_default():
    template = (
        "before\n"
        f"{context_docs.TOKEN_SAVER_START_MARKER}\n"
        "token saver content\n"
        f"{context_docs.TOKEN_SAVER_END_MARKER}\n"
        "after\n"
    )
    rendered = context_docs.render_working_rules(template)
    assert "token saver content" not in rendered
    assert "before" in rendered
    assert "after" in rendered


def test_render_working_rules_keeps_token_saver_block_when_enabled():
    template = (
        "before\n"
        f"{context_docs.TOKEN_SAVER_START_MARKER}\n"
        "token saver content\n"
        f"{context_docs.TOKEN_SAVER_END_MARKER}\n"
        "after\n"
    )
    rendered = context_docs.render_working_rules(template, token_saver=True)
    assert "token saver content" in rendered


def test_sync_runtime_docs_creates_then_updates_then_unchanged(tmp_path):
    runtime_dir = tmp_path / "runtime"
    first = context_docs.sync_runtime_docs(runtime_dir, "rules v1", engine="codex")
    assert first.action == "created"
    assert (runtime_dir / "AGENTS.md").read_text() == "rules v1"

    unchanged = context_docs.sync_runtime_docs(runtime_dir, "rules v1", engine="codex")
    assert unchanged.action == "unchanged"

    updated = context_docs.sync_runtime_docs(runtime_dir, "rules v2", engine="codex")
    assert updated.action == "updated"
    assert (runtime_dir / "AGENTS.md").read_text() == "rules v2"
    backups = list(runtime_dir.glob("AGENTS.md.bak.*"))
    assert len(backups) == 1


def test_sync_runtime_docs_uses_engine_filename(tmp_path):
    runtime_dir = tmp_path / "runtime"
    result = context_docs.sync_runtime_docs(runtime_dir, "rules", engine="claude")
    assert result.path.name == "CLAUDE.md"
