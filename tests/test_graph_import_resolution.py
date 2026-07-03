from pathlib import Path

from cdx_agent import graph


def _touch(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _isolated_repo(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(graph, "HOME_ROOT", tmp_path / "unrelated-home")
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


# --- absolute imports ------------------------------------------------------------------


def test_resolve_absolute_import_finds_module_file(tmp_path):
    repo = tmp_path / "repo"
    _touch(repo / "pkg" / "__init__.py", "")
    _touch(repo / "pkg" / "mod.py", "x = 1\n")
    target = graph._resolve_absolute_import(repo, "pkg.mod", None)
    assert target == repo / "pkg" / "mod.py"


def test_resolve_absolute_import_finds_package_init(tmp_path):
    repo = tmp_path / "repo"
    _touch(repo / "pkg" / "__init__.py", "")
    target = graph._resolve_absolute_import(repo, "pkg", None)
    assert target == repo / "pkg" / "__init__.py"


def test_resolve_absolute_import_from_import_tries_submodule(tmp_path):
    repo = tmp_path / "repo"
    _touch(repo / "pkg" / "__init__.py", "")
    _touch(repo / "pkg" / "sibling.py", "")
    # `from pkg import sibling` -- module="pkg", imported_name="sibling"
    target = graph._resolve_absolute_import(repo, "pkg", "sibling")
    assert target == repo / "pkg" / "sibling.py"


def test_resolve_absolute_import_returns_none_for_external_package(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert graph._resolve_absolute_import(repo, "numpy", None) is None


def test_resolve_absolute_import_src_layout(tmp_path):
    repo = tmp_path / "repo"
    _touch(repo / "src" / "mypkg" / "__init__.py", "")
    _touch(repo / "src" / "mypkg" / "core.py", "")
    target = graph._resolve_absolute_import(repo, "mypkg.core", None)
    assert target == repo / "src" / "mypkg" / "core.py"


# --- relative imports ------------------------------------------------------------------


def test_resolve_relative_import_sibling(tmp_path):
    repo = tmp_path / "repo"
    file_path = repo / "pkg" / "a.py"
    _touch(file_path, "")
    _touch(repo / "pkg" / "b.py", "")
    # `from . import b` inside pkg/a.py -- level=1, module=None, imported_name="b"
    target = graph._resolve_relative_import(repo, file_path, None, 1, "b")
    assert target == repo / "pkg" / "b.py"


def test_resolve_relative_import_parent_package(tmp_path):
    repo = tmp_path / "repo"
    file_path = repo / "pkg" / "sub" / "a.py"
    _touch(file_path, "")
    _touch(repo / "pkg" / "shared.py", "")
    # `from .. import shared` inside pkg/sub/a.py -- level=2
    target = graph._resolve_relative_import(repo, file_path, None, 2, "shared")
    assert target == repo / "pkg" / "shared.py"


def test_resolve_relative_import_with_module_name(tmp_path):
    repo = tmp_path / "repo"
    file_path = repo / "pkg" / "a.py"
    _touch(file_path, "")
    _touch(repo / "pkg" / "sub" / "__init__.py", "")
    _touch(repo / "pkg" / "sub" / "thing.py", "")
    # `from .sub import thing` inside pkg/a.py
    target = graph._resolve_relative_import(repo, file_path, "sub", 1, "thing")
    assert target == repo / "pkg" / "sub" / "thing.py"


# --- the core E3 regression: ambiguous same-named modules in different packages --------


def test_reverse_index_disambiguates_same_named_modules_in_different_packages(tmp_path, monkeypatch):
    repo = _isolated_repo(tmp_path, monkeypatch)
    _touch(repo / "alpha" / "__init__.py", "")
    _touch(repo / "alpha" / "utils.py", "def helper(): pass\n")
    _touch(repo / "alpha" / "consumer.py", "from alpha import utils\n")
    _touch(repo / "beta" / "__init__.py", "")
    _touch(repo / "beta" / "utils.py", "def helper(): pass\n")
    _touch(repo / "beta" / "consumer.py", "from beta import utils\n")

    result = graph.build_graph(str(repo))
    file_reverse = result["file_reverse_import_index"]

    # Each utils.py must be attributed only to its OWN consumer, not both --
    # this is exactly the ambiguity the bare-module-name index couldn't avoid.
    assert file_reverse["alpha/utils.py"] == ["alpha/consumer.py"]
    assert file_reverse["beta/utils.py"] == ["beta/consumer.py"]


def test_impact_command_uses_file_keyed_index(tmp_path, monkeypatch, capsys):
    repo = _isolated_repo(tmp_path, monkeypatch)
    _touch(repo / "alpha" / "__init__.py", "")
    _touch(repo / "alpha" / "utils.py", "def helper(): pass\n")
    _touch(repo / "alpha" / "consumer.py", "from alpha import utils\n")
    _touch(repo / "beta" / "__init__.py", "")
    _touch(repo / "beta" / "utils.py", "def helper(): pass\n")
    _touch(repo / "beta" / "consumer.py", "from beta import utils\n")

    from types import SimpleNamespace

    graph.command_impact(SimpleNamespace(repo=str(repo), files=["alpha/utils.py"], force_home_scan=False, depth=3))
    out = capsys.readouterr().out
    import json as json_mod

    payload = json_mod.loads(out)
    assert payload["alpha/utils.py"]["direct"] == ["alpha/consumer.py"]


def test_import_edges_mark_unresolved_external_imports(tmp_path, monkeypatch):
    repo = _isolated_repo(tmp_path, monkeypatch)
    _touch(repo / "main.py", "import numpy\nimport os\n")
    result = graph.build_graph(str(repo))
    node = next(n for n in result["nodes"] if n["path"] == "main.py")
    by_raw = {edge["raw"]: edge for edge in node["import_edges"]}
    assert by_raw["numpy"]["resolved"] is False
    assert by_raw["numpy"]["target"] is None
    assert by_raw["os"]["resolved"] is False


def test_import_edges_mark_resolved_local_imports(tmp_path, monkeypatch):
    repo = _isolated_repo(tmp_path, monkeypatch)
    _touch(repo / "pkg" / "__init__.py", "")
    _touch(repo / "pkg" / "core.py", "")
    _touch(repo / "main.py", "import pkg.core\n")
    result = graph.build_graph(str(repo))
    node = next(n for n in result["nodes"] if n["path"] == "main.py")
    by_raw = {edge["raw"]: edge for edge in node["import_edges"]}
    assert by_raw["pkg.core"]["resolved"] is True
    assert by_raw["pkg.core"]["target"] == "pkg/core.py"


# --- config_edges resolution -------------------------------------------------------------


def test_config_edges_drops_unresolvable_path_refs(tmp_path, monkeypatch):
    repo = _isolated_repo(tmp_path, monkeypatch)
    _touch(repo / "train.py", "CONFIG = 'configs/nonexistent_file.yaml'\n")
    result = graph.build_graph(str(repo))
    assert result["config_edges"] == []


def test_config_edges_keeps_resolvable_path_refs(tmp_path, monkeypatch):
    repo = _isolated_repo(tmp_path, monkeypatch)
    _touch(repo / "configs" / "real.yaml", "key: value\n")
    _touch(repo / "train.py", "CONFIG = 'configs/real.yaml'\n")
    result = graph.build_graph(str(repo))
    edges = result["config_edges"]
    assert {"source": "train.py", "target": "configs/real.yaml"} in edges


def test_config_edges_resolves_relative_to_referencing_file(tmp_path, monkeypatch):
    repo = _isolated_repo(tmp_path, monkeypatch)
    _touch(repo / "experiments" / "config.yaml", "key: value\n")
    _touch(repo / "experiments" / "run.py", "CONFIG = 'config.yaml'\n")
    result = graph.build_graph(str(repo))
    edges = result["config_edges"]
    assert {"source": "experiments/run.py", "target": "experiments/config.yaml"} in edges
