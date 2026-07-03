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


def test_pyproject_scripts_outrank_filename_heuristic(tmp_path, monkeypatch):
    repo = _isolated_repo(tmp_path, monkeypatch)
    _touch(
        repo / "pyproject.toml",
        '[project]\nname = "widgets"\n\n[project.scripts]\nwidget-cli = "widgets.cli:main"\n',
    )
    _touch(repo / "widgets" / "__init__.py", "")
    _touch(repo / "widgets" / "cli.py", "def main(): pass\n")
    # A filename-keyword match that has NO declared-entrypoint backing.
    _touch(repo / "run_something_unrelated.py", "x = 1\n")

    result = graph.build_graph(str(repo))
    entrypoints = {e["path"]: e for e in result["entrypoints"]}
    assert "widgets/cli.py" in entrypoints
    assert entrypoints["widgets/cli.py"]["score"] > entrypoints["run_something_unrelated.py"]["score"]
    assert "pyproject.toml" in entrypoints["widgets/cli.py"]["declared_by"]


def test_discover_pyproject_scripts_resolves_dotted_target(tmp_path, monkeypatch):
    repo = _isolated_repo(tmp_path, monkeypatch)
    _touch(
        repo / "pyproject.toml",
        '[project.scripts]\nmytool = "pkg.sub.mod:entrypoint"\n',
    )
    _touch(repo / "pkg" / "__init__.py", "")
    _touch(repo / "pkg" / "sub" / "__init__.py", "")
    _touch(repo / "pkg" / "sub" / "mod.py", "")

    declared = graph._discover_declared_entrypoints(repo)
    assert declared.get("pkg/sub/mod.py", "").startswith("pyproject.toml")


def test_discover_setup_py_console_scripts(tmp_path, monkeypatch):
    repo = _isolated_repo(tmp_path, monkeypatch)
    _touch(
        repo / "setup.py",
        "from setuptools import setup\n"
        "setup(\n"
        "    name='widgets',\n"
        "    entry_points={\n"
        "        'console_scripts': [\n"
        "            'widget-run = widgets.main:run',\n"
        "        ],\n"
        "    },\n"
        ")\n",
    )
    _touch(repo / "widgets" / "__init__.py", "")
    _touch(repo / "widgets" / "main.py", "")

    declared = graph._discover_declared_entrypoints(repo)
    assert declared.get("widgets/main.py", "").startswith("setup.py")


def test_discover_makefile_targets(tmp_path, monkeypatch):
    repo = _isolated_repo(tmp_path, monkeypatch)
    _touch(repo / "Makefile", "train:\n\tpython train.py --epochs 10\n")
    _touch(repo / "train.py", "")

    declared = graph._discover_declared_entrypoints(repo)
    assert declared.get("train.py", "").startswith("Makefile")


def test_discover_ci_run_steps(tmp_path, monkeypatch):
    repo = _isolated_repo(tmp_path, monkeypatch)
    _touch(
        repo / ".github" / "workflows" / "ci.yml",
        "jobs:\n  test:\n    steps:\n      - run: python eval.py --quick\n",
    )
    _touch(repo / "eval.py", "")

    declared = graph._discover_declared_entrypoints(repo)
    assert declared.get("eval.py", "").startswith("CI run step")


def test_likely_entrypoints_without_repo_still_works(tmp_path, monkeypatch):
    # backward compat: repo=None should just skip declared-entrypoint discovery
    repo = _isolated_repo(tmp_path, monkeypatch)
    _touch(repo / "train.py", "def main(): pass\n")
    result = graph.build_graph(str(repo))
    node = graph.FileNode(**next(n for n in result["nodes"] if n["path"] == "train.py"))
    entries = graph._likely_entrypoints([node])
    assert entries and "declared_by" not in entries[0]


def test_pyproject_script_target_pointing_outside_repo_is_dropped(tmp_path, monkeypatch):
    repo = _isolated_repo(tmp_path, monkeypatch)
    _touch(repo / "pyproject.toml", '[project.scripts]\nmytool = "numpy.cli:main"\n')
    declared = graph._discover_declared_entrypoints(repo)
    assert declared == {}
