import pytest

from cdx_agent import config as config_mod
from cdx_agent import workspace_mirror as wm


def _cfg(tmp_path):
    return config_mod.Config.defaults(home=tmp_path / "home")


def _repo(tmp_path, name):
    repo = tmp_path / name
    repo.mkdir(parents=True)
    return repo


def test_validate_workspace_name():
    assert wm.validate_workspace_name("my-workspace_1") is True
    assert wm.validate_workspace_name("") is False
    assert wm.validate_workspace_name(".") is False
    assert wm.validate_workspace_name("a/b") is False


def test_workspace_name_from_spec_uses_basename_for_file(tmp_path):
    manifest = tmp_path / "primary.paths"
    manifest.write_text("")
    assert wm.workspace_name_from_spec(str(manifest)) == "primary"


def test_workspace_name_from_spec_validates_bare_name():
    assert wm.workspace_name_from_spec("myws") == "myws"
    with pytest.raises(ValueError):
        wm.workspace_name_from_spec("bad/name")


def test_workspace_manifest_path_for_named_workspace(tmp_path):
    cfg = _cfg(tmp_path)
    path = wm.workspace_manifest_path(cfg, "myws")
    assert path == cfg.workspace_manifest_root / "myws.paths"


def test_workspace_entries_skips_blank_and_comment_lines(tmp_path):
    repo_a = _repo(tmp_path, "repo_a")
    manifest = tmp_path / "ws.paths"
    manifest.write_text(f"# comment\n\n{repo_a}\n  \nrelative_dir\n")
    (tmp_path / "relative_dir").mkdir()

    entries = wm.workspace_entries(manifest)
    assert len(entries) == 2
    assert entries[0].resolved == repo_a.resolve()
    assert entries[1].resolved == (tmp_path / "relative_dir").resolve()


def test_init_workspace_creates_manifest(tmp_path):
    cfg = _cfg(tmp_path)
    repo_a = _repo(tmp_path, "repo_a")
    repo_b = _repo(tmp_path, "repo_b")

    result = wm.init_workspace(cfg, "myws", [repo_a, repo_b])
    assert result.action == "created"
    assert result.manifest.is_file()
    content = result.manifest.read_text()
    assert str(repo_a.resolve()) in content
    assert str(repo_b.resolve()) in content


def test_init_workspace_refuses_overwrite_without_force(tmp_path):
    cfg = _cfg(tmp_path)
    repo_a = _repo(tmp_path, "repo_a")
    wm.init_workspace(cfg, "myws", [repo_a])
    with pytest.raises(ValueError):
        wm.init_workspace(cfg, "myws", [repo_a])


def test_init_workspace_force_backs_up_existing(tmp_path):
    cfg = _cfg(tmp_path)
    repo_a = _repo(tmp_path, "repo_a")
    repo_b = _repo(tmp_path, "repo_b")
    wm.init_workspace(cfg, "myws", [repo_a])
    wm.init_workspace(cfg, "myws", [repo_b], force=True)
    backups = list(cfg.workspace_manifest_root.glob("myws.paths.bak.*"))
    assert len(backups) == 1


def test_init_workspace_dry_run_does_not_write(tmp_path):
    cfg = _cfg(tmp_path)
    repo_a = _repo(tmp_path, "repo_a")
    result = wm.init_workspace(cfg, "myws", [repo_a], dry_run=True)
    assert result.action == "would_create"
    assert not result.manifest.exists()


def test_init_workspace_rejects_nonexistent_path(tmp_path):
    cfg = _cfg(tmp_path)
    with pytest.raises(ValueError):
        wm.init_workspace(cfg, "myws", [tmp_path / "does-not-exist"])


def test_list_workspaces(tmp_path):
    cfg = _cfg(tmp_path)
    repo_a = _repo(tmp_path, "repo_a")
    assert wm.list_workspaces(cfg) == []
    wm.init_workspace(cfg, "myws", [repo_a])
    listed = wm.list_workspaces(cfg)
    assert listed[0][0] == "myws"


def test_show_workspace_reports_presence(tmp_path):
    cfg = _cfg(tmp_path)
    repo_a = _repo(tmp_path, "repo_a")
    wm.init_workspace(cfg, "myws", [repo_a])
    shutil_removed = repo_a
    entries = wm.show_workspace(cfg, "myws")
    assert entries[0].present is True
    import shutil

    shutil.rmtree(shutil_removed)
    entries_after = wm.show_workspace(cfg, "myws")
    assert entries_after[0].present is False


# --- mirror building + containment guard ------------------------------------------------


def test_build_mirror_creates_symlinks_and_index(tmp_path):
    cfg = _cfg(tmp_path)
    repo_a = _repo(tmp_path, "repo_a")
    repo_b = _repo(tmp_path, "repo_b")
    wm.init_workspace(cfg, "myws", [repo_a, repo_b])

    result = wm.build_mirror(cfg, "myws")
    assert result.mirror_root.is_dir()
    assert len(result.links) == 2
    for link in result.links:
        assert (result.mirror_root / link.link_name).is_symlink()
    assert result.index_path.is_file()
    assert "Warning: editing files through this mirror" in result.index_path.read_text()


def test_build_mirror_deduplicates_colliding_basenames(tmp_path):
    cfg = _cfg(tmp_path)
    repo_1 = _repo(tmp_path / "group1", "shared_name")
    repo_2 = _repo(tmp_path / "group2", "shared_name")
    wm.init_workspace(cfg, "myws", [repo_1, repo_2])

    result = wm.build_mirror(cfg, "myws")
    names = {link.link_name for link in result.links}
    assert names == {"shared_name", "shared_name-2"}


def test_build_mirror_refuses_home_like_entry(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    cfg = config_mod.Config.defaults(home=home)
    wm.init_workspace(cfg, "myws", [home])
    with pytest.raises(ValueError):
        wm.build_mirror(cfg, "myws")


def test_build_mirror_dry_run_does_not_touch_filesystem(tmp_path):
    cfg = _cfg(tmp_path)
    repo_a = _repo(tmp_path, "repo_a")
    wm.init_workspace(cfg, "myws", [repo_a])
    result = wm.build_mirror(cfg, "myws", dry_run=True)
    assert not result.mirror_root.exists()


def test_build_mirror_rebuilds_cleanly_on_rerun(tmp_path):
    cfg = _cfg(tmp_path)
    repo_a = _repo(tmp_path, "repo_a")
    wm.init_workspace(cfg, "myws", [repo_a])
    wm.build_mirror(cfg, "myws")
    stray = wm.workspace_mirror_path(cfg, "myws") / "stray_file.txt"
    stray.write_text("leftover")
    wm.build_mirror(cfg, "myws")
    assert not stray.exists()


def test_safe_remove_tree_refuses_outside_root(tmp_path):
    cfg = _cfg(tmp_path)
    outside = tmp_path / "not_mirror_root" / "victim"
    outside.mkdir(parents=True)
    with pytest.raises(ValueError):
        wm.safe_remove_tree(cfg, outside)
    assert outside.exists()


def test_safe_remove_tree_allows_inside_mirror_root(tmp_path):
    cfg = _cfg(tmp_path)
    target = cfg.workspace_mirror_root / "myws"
    target.mkdir(parents=True)
    wm.safe_remove_tree(cfg, target)
    assert not target.exists()


def test_clean_workspace_removes_mirror(tmp_path):
    cfg = _cfg(tmp_path)
    repo_a = _repo(tmp_path, "repo_a")
    wm.init_workspace(cfg, "myws", [repo_a])
    wm.build_mirror(cfg, "myws")
    removed = wm.clean_workspace(cfg, "myws")
    assert removed is not None
    assert not removed.exists()


def test_clean_workspace_noop_when_no_mirror(tmp_path):
    cfg = _cfg(tmp_path)
    assert wm.clean_workspace(cfg, "nonexistent") is None


def test_resolve_dg_root_refuses_home_dir(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    cfg = config_mod.Config.defaults(home=home)
    with pytest.raises(ValueError):
        wm.resolve_dg_root(cfg, home)


def test_resolve_dg_root_allows_with_force(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    cfg = config_mod.Config.defaults(home=home)
    resolved = wm.resolve_dg_root(cfg, home, force_home_scan=True)
    assert resolved == home.resolve()


def test_need_dg_returns_none_when_absent(monkeypatch):
    monkeypatch.setattr(wm.shutil, "which", lambda name: None)
    assert wm.need_dg() is None


def test_build_dg_command_shape(tmp_path):
    cmd = wm.build_dg_command("/usr/bin/dg", tmp_path, ["--flag"])
    assert cmd == ["/usr/bin/dg", str(tmp_path), "--flag"]


def test_unique_link_name_survives_literal_dash_n_entry():
    # Regression: entries sanitizing to ["foo-2", "foo", "foo"] used to issue
    # "foo-2" twice (counter ignored already-issued literal names), raising
    # FileExistsError mid-build.
    from cdx_agent.workspace_mirror import _unique_link_name

    seen: dict[str, int] = {}
    issued = [
        _unique_link_name("foo-2", seen),
        _unique_link_name("foo", seen),
        _unique_link_name("foo", seen),
        _unique_link_name("foo", seen),
    ]
    assert len(issued) == len(set(issued)), issued
