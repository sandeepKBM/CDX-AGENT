import json
import os
import subprocess

import pytest

from cdx_agent import project


def test_load_project_routes_and_long_suites(tmp_path):
    cdx = tmp_path / ".cdx"
    cdx.mkdir()
    (cdx / "project.json").write_text(json.dumps({"entry": "run.py", "ledger": "L.md"}))
    (cdx / "test_routes.yaml").write_text(
        'routes:\n  - {glob: "a/**/test_*.py", host_class: westeros, reason: r}\ndefault: {host_class: [local]}\n')
    (cdx / "long_suites.txt").write_text("# c\ntests/a.py local-only # x\ntests/b_*.py\n")
    assert project.load_project(tmp_path)["entry"] == "run.py"
    routes = project.load_routes(tmp_path)
    assert routes["routes"][0]["host_class"] == ["westeros"]
    assert routes["default"]["host_class"] == ["local"]
    assert project.long_suites(tmp_path) == [("tests/a.py", True), ("tests/b_*.py", False)]


def test_defaults_when_missing(tmp_path):
    assert project.load_project(tmp_path) == {}
    assert project.load_routes(tmp_path)["routes"] == []
    assert project.long_suites(tmp_path) == []


def test_bad_route_raises(tmp_path):
    (tmp_path / ".cdx").mkdir()
    (tmp_path / ".cdx" / "test_routes.yaml").write_text("routes:\n  - {glob: x}\n")
    with pytest.raises(ValueError):
        project.load_routes(tmp_path)


def test_worktree_falls_back_to_main_cdx(tmp_path):
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    main = tmp_path / "main"
    main.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=main, check=True, env=env)
    (main / ".cdx").mkdir()
    (main / ".cdx" / "project.json").write_text('{"entry": "e"}')
    (main / "f").write_text("x")
    subprocess.run(["git", "add", "f"], cwd=main, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "i"], cwd=main, check=True, env=env)
    wt = tmp_path / "wt"
    subprocess.run(["git", "worktree", "add", "-q", "-b", "b", str(wt)], cwd=main, check=True, env=env)
    assert not (wt / ".cdx").exists()
    assert project.cdx_dir(wt) == main.resolve() / ".cdx"
    assert project.load_project(wt)["entry"] == "e"
