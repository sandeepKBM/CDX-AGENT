from cdx_agent import config as config_mod


def test_defaults_derive_from_home_not_literals(monkeypatch, tmp_path):
    fake_home = tmp_path / "home" / "someone"
    fake_home.mkdir(parents=True)
    cfg = config_mod.Config.defaults(home=fake_home)
    assert cfg.user_root == fake_home
    assert cfg.account_home == fake_home
    assert cfg.tools_root == fake_home / "codex_tools"
    assert cfg.runtime_root == fake_home / "codex_runtime"
    assert str(cfg.user_root) != "/common/users/ss5772"


def test_load_config_precedence_explicit_path_wins(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(config_mod.Path, "home", staticmethod(lambda: home))

    env_config = tmp_path / "env_config.yaml"
    env_config.write_text("user_root: /env-root\n")
    monkeypatch.setenv(config_mod.ENV_CONFIG_PATH, str(env_config))

    explicit_config = tmp_path / "explicit_config.yaml"
    explicit_config.write_text("user_root: /explicit-root\n")

    cfg = config_mod.load_config(explicit_path=explicit_config)
    assert str(cfg.user_root) == "/explicit-root"


def test_load_config_env_var_used_when_no_explicit_path(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(config_mod.Path, "home", staticmethod(lambda: home))

    env_config = tmp_path / "env_config.yaml"
    env_config.write_text("user_root: /env-root\nstale_retention_days: 30\n")
    monkeypatch.setenv(config_mod.ENV_CONFIG_PATH, str(env_config))

    cfg = config_mod.load_config()
    assert str(cfg.user_root) == "/env-root"
    assert cfg.stale_retention_days == 30


def test_load_config_falls_back_to_defaults_when_nothing_present(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(config_mod.Path, "home", staticmethod(lambda: home))
    monkeypatch.delenv(config_mod.ENV_CONFIG_PATH, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-such-config-dir"))

    cfg = config_mod.load_config()
    assert cfg.user_root == home


def test_interpolation_resolves_home_and_user_tokens(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(config_mod.Path, "home", staticmethod(lambda: home))
    monkeypatch.setenv("USER", "alice")

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "user_root: ${HOME}\n"
        "tools_root: ${user_root}/custom_tools\n"
    )
    cfg = config_mod.load_config(explicit_path=cfg_path)
    assert cfg.user_root == home
    assert cfg.tools_root == home / "custom_tools"


def test_write_config_then_load_round_trips(tmp_path):
    cfg = config_mod.Config.defaults(home=tmp_path / "home")
    dest = tmp_path / "config.yaml"
    config_mod.write_config(cfg, dest)
    loaded = config_mod.load_config(explicit_path=dest)
    assert loaded.user_root == cfg.user_root
    assert loaded.tools_root == cfg.tools_root
    assert loaded.skill_roots == cfg.skill_roots


def test_is_home_like_dir_catches_exact_match(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod.Path, "home", staticmethod(lambda: tmp_path))
    cfg = config_mod.Config.defaults(home=tmp_path)
    assert config_mod.is_home_like_dir(tmp_path, cfg) is True


def test_is_home_like_dir_catches_subdirs(tmp_path, monkeypatch):
    # The bash predecessor only matched home exactly; a subdirectory of home
    # slipped through and could be graph-scanned/mirrored accidentally. This
    # is the fix: any descendant of a home-like root is also caught.
    monkeypatch.setattr(config_mod.Path, "home", staticmethod(lambda: tmp_path))
    cfg = config_mod.Config.defaults(home=tmp_path)
    nested = tmp_path / "some" / "nested" / "dir"
    nested.mkdir(parents=True)
    assert config_mod.is_home_like_dir(nested, cfg) is True


def test_is_home_like_dir_allows_unrelated_dir(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(config_mod.Path, "home", staticmethod(lambda: fake_home))
    cfg = config_mod.Config.defaults(home=fake_home)
    unrelated = tmp_path / "elsewhere" / "repo"
    unrelated.mkdir(parents=True)
    assert config_mod.is_home_like_dir(unrelated, cfg) is False


def test_repo_slug_is_stable_and_path_derived(tmp_path):
    repo_a = tmp_path / "repo_a"
    repo_a.mkdir()
    repo_b = tmp_path / "repo_b"
    repo_b.mkdir()
    slug_a1 = config_mod.repo_slug(repo_a)
    slug_a2 = config_mod.repo_slug(repo_a)
    slug_b = config_mod.repo_slug(repo_b)
    assert slug_a1 == slug_a2
    assert slug_a1 != slug_b
    assert slug_a1.startswith("repo_a__")


def test_looks_like_project_dir_detects_markers(tmp_path):
    assert config_mod.looks_like_project_dir(tmp_path) is False
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    assert config_mod.looks_like_project_dir(tmp_path) is True


def test_backup_path_copies_and_timestamps(tmp_path):
    target = tmp_path / "AGENTS.md"
    target.write_text("hello")
    backup = config_mod.backup_path(target)
    assert backup is not None
    assert backup.read_text() == "hello"
    assert backup.name.startswith("AGENTS.md.bak.")


def test_backup_path_returns_none_for_missing_file(tmp_path):
    assert config_mod.backup_path(tmp_path / "does-not-exist") is None
