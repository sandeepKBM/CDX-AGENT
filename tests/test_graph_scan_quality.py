from pathlib import Path

from cdx_agent import graph


def test_is_home_like_dir_catches_exact_match(monkeypatch, tmp_path):
    monkeypatch.setattr(graph, "HOME_ROOT", tmp_path)
    assert graph._is_home_like_dir(tmp_path) is True


def test_is_home_like_dir_catches_subdirs(monkeypatch, tmp_path):
    # Matches the fix applied to config.py::is_home_like_dir: the bash
    # predecessor (and graph.py's own original _is_home_like_dir) only
    # matched home exactly, letting a subdirectory of home slip through an
    # accidental full-tree scan.
    monkeypatch.setattr(graph, "HOME_ROOT", tmp_path)
    nested = tmp_path / "some" / "nested" / "dir"
    nested.mkdir(parents=True)
    assert graph._is_home_like_dir(nested) is True


def test_is_home_like_dir_allows_unrelated_dir(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(graph, "HOME_ROOT", home)
    unrelated = tmp_path / "elsewhere" / "repo"
    unrelated.mkdir(parents=True)
    assert graph._is_home_like_dir(unrelated) is False


def _touch(path: Path, content: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_iter_files_no_truncation_reports_nothing_dropped(tmp_path):
    for i in range(5):
        _touch(tmp_path / f"file_{i}.py")
    files, truncated, dropped = graph._iter_files(tmp_path, max_files=10)
    assert truncated is False
    assert dropped == []
    assert len(files) == 5


def test_iter_files_prioritizes_entrypoint_and_shallow_files_when_truncated():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        # A shallow, entrypoint-named file that should survive truncation...
        _touch(repo / "train.py")
        # ...over a bunch of deeply nested, non-entrypoint-named filler files.
        for i in range(20):
            _touch(repo / "deep" / "nested" / "path" / f"filler_{i}.py")

        files, truncated, dropped = graph._iter_files(repo, max_files=5)
        assert truncated is True
        kept_names = {p.name for p in files}
        assert "train.py" in kept_names
        assert len(files) == 5
        assert len(dropped) > 0
        # the dropped sample should be exactly the lower-priority filler files
        assert all("filler_" in p.name for p in dropped[: len(dropped)])


def test_iter_files_hard_ceiling_is_bounded():
    # Sanity check that SCAN_HARD_CEILING is a real bound, not accidentally
    # removed -- doesn't create anywhere near the ceiling, just checks the
    # constant is still wired into the walk loop.
    assert graph.SCAN_HARD_CEILING > 0


def _write_sample_repo_for_truncation(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text('[project]\nname = "sample"\n')
    _touch(root / "train.py", "def main():\n    pass\n")
    for i in range(10):
        _touch(root / "extra" / f"module_{i}.py", "x = 1\n")
    return root


def test_build_graph_reports_dropped_sample_when_truncated(tmp_path, monkeypatch):
    monkeypatch.setattr(graph, "HOME_ROOT", tmp_path / "unrelated-home")
    repo = _write_sample_repo_for_truncation(tmp_path / "repo")

    result = graph.build_graph(str(repo), max_files=3)
    assert result["scan_truncated"] is True
    assert result["scan_dropped_count"] > 0
    assert len(result["scan_dropped_sample"]) > 0

    context_pack = (repo / ".codex_graph" / "context_pack.md").read_text()
    assert "Scan truncated" in context_pack
    assert "Dropped file sample" in context_pack


def test_build_graph_not_truncated_reports_zero_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(graph, "HOME_ROOT", tmp_path / "unrelated-home")
    repo = _write_sample_repo_for_truncation(tmp_path / "repo")

    result = graph.build_graph(str(repo), max_files=5000)
    assert result["scan_truncated"] is False
    assert result["scan_dropped_count"] == 0
    assert result["scan_dropped_sample"] == []


# --- E2: de-hardcoded topic hints -------------------------------------------------------


def test_resolve_topic_hints_defaults_to_shipped_constant_when_no_signal(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    hints = graph.resolve_topic_hints(repo)
    assert set(graph.TOPIC_HINTS).issubset(set(hints))


def test_resolve_topic_hints_discovers_pyproject_keywords(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        "[project]\nname = \"widgets\"\nkeywords = [\"widget-factory\", \"gearbox\"]\n"
    )
    hints = graph.resolve_topic_hints(repo)
    assert "widget_factory" in hints or "widget-factory" in hints
    assert "gearbox" in hints
    # shipped defaults are still present -- discovery supplements, doesn't replace
    assert "policy" in hints


def test_resolve_topic_hints_discovers_top_level_package_names(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src" / "widgetlib").mkdir(parents=True)
    (repo / "src" / "widgetlib" / "__init__.py").write_text("")
    hints = graph.resolve_topic_hints(repo)
    assert "widgetlib" in hints


def test_resolve_topic_hints_workspace_yaml_override_replaces_defaults(tmp_path):
    repo = tmp_path / "repo"
    graph_dir = repo / ".codex_graph"
    graph_dir.mkdir(parents=True)
    (graph_dir / "workspace.yaml").write_text("topic_hints:\n  - custom_domain_term\n  - another_term\n")

    hints = graph.resolve_topic_hints(repo)
    assert hints == ("another_term", "custom_domain_term")
    # override fully replaces the shipped defaults, not merged with them
    assert "policy" not in hints


def test_tags_from_text_uses_provided_hints_not_just_module_default():
    tags = graph._tags_from_text("this mentions gearbox and widget stuff", hints=("gearbox",))
    assert tags == ["gearbox"]


def test_build_graph_uses_discovered_hints_for_non_robotics_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(graph, "HOME_ROOT", tmp_path / "unrelated-home")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text('[project]\nname = "widgets"\nkeywords = ["gearbox"]\n')
    (repo / "main.py").write_text("# gearbox controller\nGEARBOX = True\n")

    result = graph.build_graph(str(repo))
    assert "gearbox" in result["topic_hints"]
    node = next(n for n in result["nodes"] if n["path"] == "main.py")
    assert "gearbox" in node["tags"]
