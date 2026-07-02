from cdx_agent import config as config_mod
from cdx_agent import onboarding


def test_init_user_creates_config_and_skeleton(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    config_path = tmp_path / "config.yaml"

    result = onboarding.init_user(account_home=home, config_path=config_path)
    assert result.config_path.is_file()
    loaded = config_mod.load_config(explicit_path=config_path)
    assert loaded.user_root == home

    for directory in result.created_dirs:
        assert directory.is_dir()


def test_init_user_seeds_builtin_defaults_when_no_adoption(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    config_path = tmp_path / "config.yaml"

    result = onboarding.init_user(account_home=home, config_path=config_path)
    seeded_names = {p.name for p in result.seeded_files}
    assert "AGENTS.md" in seeded_names
    assert "pre_tool_use_policy.py" in seeded_names
    for f in result.seeded_files:
        assert f.is_file()


def test_init_user_is_idempotent_does_not_reseed(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    config_path = tmp_path / "config.yaml"

    first = onboarding.init_user(account_home=home, config_path=config_path)
    assert len(first.seeded_files) > 0
    second = onboarding.init_user(account_home=home, config_path=config_path)
    assert second.seeded_files == ()


def test_init_user_dry_run_creates_nothing(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    config_path = tmp_path / "config.yaml"

    result = onboarding.init_user(account_home=home, config_path=config_path, dry_run=True)
    assert not config_path.exists()
    for directory in result.created_dirs:
        assert not directory.exists()


def test_init_user_custom_user_root_recomputes_derived_paths(tmp_path):
    # Regression test: overriding user_root alone (distinct from
    # account_home, e.g. a shared HPC work filesystem vs. a quota-limited
    # $HOME) must cascade into runtime_root/workspace_manifest_root/
    # workspace_mirror_root/tools_root -- previously only the `user_root`
    # field itself was replaced, leaving those derived fields silently
    # pointing at account_home instead.
    home = tmp_path / "home"
    home.mkdir()
    work_root = tmp_path / "work_root"
    config_path = tmp_path / "config.yaml"

    result = onboarding.init_user(account_home=home, user_root=work_root, config_path=config_path)
    cfg = result.config
    assert cfg.user_root == work_root
    assert cfg.account_home == home
    assert cfg.runtime_root == work_root / "codex_runtime"
    assert cfg.workspace_manifest_root == work_root / ".cdx" / "workspaces"
    assert cfg.workspace_mirror_root == work_root / ".cdx" / "dg_workspaces"
    assert cfg.tools_root == work_root / "codex_tools"
    assert work_root / "codex_tools" / "skills" in cfg.skill_roots
    # account_home-derived paths must be unaffected
    assert home / ".agents" / "skills" in cfg.skill_roots


def test_init_user_custom_tools_root_updates_skill_roots(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    custom_tools = tmp_path / "shared_tools"
    config_path = tmp_path / "config.yaml"

    result = onboarding.init_user(account_home=home, tools_root=custom_tools, config_path=config_path)
    assert result.config.tools_root == custom_tools
    assert custom_tools / "skills" in result.config.skill_roots
    assert (custom_tools / "base" / "AGENTS.md").is_file()


def test_init_user_adopts_from_existing_tools_root(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    existing_tools = tmp_path / "existing_tools"
    (existing_tools / "skills" / "cool-skill").mkdir(parents=True)
    (existing_tools / "skills" / "cool-skill" / "SKILL.md").write_text(
        "---\nname: cool-skill\ndescription: A perfectly good skill for testing adoption flows.\n---\n\nDo cool things.\n"
    )
    (existing_tools / "base").mkdir()
    (existing_tools / "base" / "AGENTS.md").write_text("existing agents content\n")

    config_path = tmp_path / "config.yaml"
    result = onboarding.init_user(
        account_home=home, from_existing_user_tools_root=existing_tools, config_path=config_path
    )
    assert (result.config.tools_root / "skills" / "cool-skill" / "SKILL.md").is_file()
    assert (result.config.tools_root / "base" / "AGENTS.md").read_text() == "existing agents content\n"
    # adoption path does not additionally seed the tiny built-in defaults
    assert not any(f.name == "pre_tool_use_policy.py" for f in result.seeded_files)


def test_init_user_adoption_does_not_overwrite_dirs_with_real_content(tmp_path):
    # The skeleton step pre-creates every adoptable subdir as an *empty*
    # directory, so an adoption must key off actual content, not mere
    # existence -- otherwise it would either wrongly skip real adoptions
    # (content never gets copied) or wrongly clobber a directory the user
    # already populated by hand or via a prior adoption.
    home = tmp_path / "home"
    home.mkdir()
    config_path = tmp_path / "config.yaml"

    first_source = tmp_path / "first_source"
    (first_source / "skills" / "first-skill").mkdir(parents=True)

    first_result = onboarding.init_user(
        account_home=home, from_existing_user_tools_root=first_source, config_path=config_path
    )
    assert (first_result.config.tools_root / "skills" / "first-skill").exists()

    second_source = tmp_path / "second_source"
    (second_source / "skills" / "second-skill").mkdir(parents=True)

    second_result = onboarding.init_user(
        account_home=home, from_existing_user_tools_root=second_source, config_path=config_path
    )
    # "skills" already has real content from the first adoption -- the
    # second adoption must not touch it.
    assert (second_result.config.tools_root / "skills" / "first-skill").exists()
    assert not (second_result.config.tools_root / "skills" / "second-skill").exists()
