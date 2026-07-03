from cdx_agent import graph


def _isolated_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(graph, "HOME_ROOT", tmp_path / "unrelated-home")
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


def test_build_graph_writes_scan_cache_file(tmp_path, monkeypatch):
    repo = _isolated_repo(tmp_path, monkeypatch)
    (repo / "main.py").write_text("x = 1\n")
    graph.build_graph(str(repo))
    cache_path = repo / ".codex_graph" / ".scan_cache.json"
    assert cache_path.is_file()


def test_scan_repository_reuses_cache_for_unchanged_file(tmp_path, monkeypatch):
    repo = _isolated_repo(tmp_path, monkeypatch)
    (repo / "main.py").write_text("import os\n")

    graph._scan_repository(repo, max_files=100)

    calls = {"count": 0}
    original = graph._scan_python

    def counting_scan_python(path, repo_arg, hints=graph.TOPIC_HINTS):
        calls["count"] += 1
        return original(path, repo_arg, hints)

    monkeypatch.setattr(graph, "_scan_python", counting_scan_python)
    nodes, parse_errors, files, truncated, dropped = graph._scan_repository(repo, max_files=100)
    assert calls["count"] == 0  # served entirely from cache
    assert len(nodes) == 1
    assert nodes[0].path == "main.py"


def test_scan_repository_detects_changed_file_content(tmp_path, monkeypatch):
    repo = _isolated_repo(tmp_path, monkeypatch)
    target = repo / "main.py"
    target.write_text("import os\n")
    graph._scan_repository(repo, max_files=100)

    # Different length so (mtime, size) is guaranteed to differ even within
    # the same filesystem mtime tick.
    target.write_text("import os\nimport sys\nimport json\n")

    calls = {"count": 0}
    original = graph._scan_python

    def counting_scan_python(path, repo_arg, hints=graph.TOPIC_HINTS):
        calls["count"] += 1
        return original(path, repo_arg, hints)

    monkeypatch.setattr(graph, "_scan_python", counting_scan_python)
    nodes, parse_errors, files, truncated, dropped = graph._scan_repository(repo, max_files=100)
    assert calls["count"] == 1  # re-scanned because size/mtime changed
    node = next(n for n in nodes if n.path == "main.py")
    assert "sys" in node.imports
    assert "json" in node.imports


def test_scan_repository_detects_new_and_removed_files(tmp_path, monkeypatch):
    repo = _isolated_repo(tmp_path, monkeypatch)
    (repo / "a.py").write_text("x = 1\n")
    nodes1, *_ = graph._scan_repository(repo, max_files=100)
    assert {n.path for n in nodes1} == {"a.py"}

    (repo / "a.py").unlink()
    (repo / "b.py").write_text("y = 2\n")
    nodes2, *_ = graph._scan_repository(repo, max_files=100)
    assert {n.path for n in nodes2} == {"b.py"}


def test_scan_repository_cache_invalidated_by_topic_hints_change(tmp_path, monkeypatch):
    repo = _isolated_repo(tmp_path, monkeypatch)
    (repo / "main.py").write_text("gearbox = True\n")
    graph._scan_repository(repo, max_files=100, topic_hints=("gearbox",))

    calls = {"count": 0}
    original = graph._scan_python

    def counting_scan_python(path, repo_arg, hints=graph.TOPIC_HINTS):
        calls["count"] += 1
        return original(path, repo_arg, hints)

    monkeypatch.setattr(graph, "_scan_python", counting_scan_python)
    nodes, *_ = graph._scan_repository(repo, max_files=100, topic_hints=("widget",))
    # different hints -> cache is bypassed entirely, file is re-scanned
    assert calls["count"] == 1
    node = next(n for n in nodes if n.path == "main.py")
    assert "gearbox" not in node.tags


def test_scan_repository_use_cache_false_always_rescans(tmp_path, monkeypatch):
    repo = _isolated_repo(tmp_path, monkeypatch)
    (repo / "main.py").write_text("x = 1\n")
    graph._scan_repository(repo, max_files=100, use_cache=True)

    calls = {"count": 0}
    original = graph._scan_python

    def counting_scan_python(path, repo_arg, hints=graph.TOPIC_HINTS):
        calls["count"] += 1
        return original(path, repo_arg, hints)

    monkeypatch.setattr(graph, "_scan_python", counting_scan_python)
    graph._scan_repository(repo, max_files=100, use_cache=False)
    assert calls["count"] == 1


def test_scan_cache_survives_across_build_graph_calls(tmp_path, monkeypatch):
    repo = _isolated_repo(tmp_path, monkeypatch)
    (repo / "main.py").write_text("import os\n")
    result1 = graph.build_graph(str(repo))
    result2 = graph.build_graph(str(repo))
    assert result1["node_count"] == result2["node_count"] == 1


def test_corrupt_scan_cache_is_ignored_not_fatal(tmp_path, monkeypatch):
    repo = _isolated_repo(tmp_path, monkeypatch)
    (repo / "main.py").write_text("x = 1\n")
    cache_dir = repo / ".codex_graph"
    cache_dir.mkdir(parents=True)
    (cache_dir / ".scan_cache.json").write_text("not valid json{{{")

    nodes, *_ = graph._scan_repository(repo, max_files=100)
    assert len(nodes) == 1


def test_scan_cache_ignores_stale_entries_for_deleted_files_on_next_save(tmp_path, monkeypatch):
    repo = _isolated_repo(tmp_path, monkeypatch)
    (repo / "a.py").write_text("x = 1\n")
    graph._scan_repository(repo, max_files=100)
    (repo / "a.py").unlink()
    (repo / "b.py").write_text("y = 2\n")
    graph._scan_repository(repo, max_files=100)

    cache_path = repo / ".codex_graph" / ".scan_cache.json"
    import json

    payload = json.loads(cache_path.read_text())
    assert "a.py" not in payload["entries"]
    assert "b.py" in payload["entries"]
