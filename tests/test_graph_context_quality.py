"""Tests for the context-engine quality upgrades: config ranking, config-edge
de-noising, real import call chains, no-task relevance fallback, stale-pack
detection, transitive --impact, and the context-budget artifact report.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

from cdx_agent import graph, token_tools


def _touch(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _isolated_repo(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(graph, "HOME_ROOT", tmp_path / "unrelated-home")
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


# --- config ranking ---------------------------------------------------------------


def test_real_configs_outrank_vendored_and_report_noise(tmp_path, monkeypatch):
    repo = _isolated_repo(tmp_path, monkeypatch)
    _touch(repo / "configs" / "train.yaml", "lr: 3e-4\n")
    _touch(repo / "pyproject.toml", "[project]\nname = 'x'\n")
    _touch(repo / "third_party" / "vendored" / "index.json", "{}")
    _touch(repo / "storage_benchmark_report.json", "{}")
    _touch(repo / "loader.py", "import yaml\ncfg = 'configs/train.yaml'\n")

    graph.build_graph(str(repo))
    pack = (repo / ".codex_graph" / "context_pack.md").read_text()
    section = pack.split("## Important configs")[1].split("## Risky folders")[0]
    assert "configs/train.yaml" in section
    assert "pyproject.toml" in section
    assert "third_party/vendored/index.json" not in section
    assert "storage_benchmark_report.json" not in section


# --- config-edge de-noise ---------------------------------------------------------


def test_data_manifest_json_produces_no_config_edges(tmp_path, monkeypatch):
    repo = _isolated_repo(tmp_path, monkeypatch)
    targets = [f"data_{i}.json" for i in range(15)]
    for name in targets:
        _touch(repo / name, "{}")
    # A report/manifest-named json listing many real paths is lineage, not config wiring.
    _touch(repo / "cleanup_manifest.json", json.dumps({"files": targets}))
    result = graph.build_graph(str(repo))
    sources = {edge["source"] for edge in result["config_edges"]}
    assert "cleanup_manifest.json" not in sources


def test_prolific_source_capped_as_data_manifest(tmp_path, monkeypatch):
    repo = _isolated_repo(tmp_path, monkeypatch)
    targets = [f"cfg_{i}.yaml" for i in range(graph.MAX_CONFIG_EDGES_PER_SOURCE + 5)]
    for name in targets:
        _touch(repo / name, "a: 1\n")
    _touch(repo / "lister.py", "PATHS = " + json.dumps(targets) + "\n")
    result = graph.build_graph(str(repo))
    sources = {edge["source"] for edge in result["config_edges"]}
    assert "lister.py" not in sources  # exceeded per-source cap -> dropped wholesale


def test_modest_config_references_survive(tmp_path, monkeypatch):
    repo = _isolated_repo(tmp_path, monkeypatch)
    _touch(repo / "config.yaml", "a: 1\n")
    _touch(repo / "train.py", "CONFIG = 'config.yaml'\n")
    result = graph.build_graph(str(repo))
    assert {"source": "train.py", "target": "config.yaml"} in result["config_edges"]


def test_nested_git_repo_is_scan_boundary(tmp_path, monkeypatch):
    repo = _isolated_repo(tmp_path, monkeypatch)
    _touch(repo / "main.py", "x = 1\n")
    nested = repo / "third_party" / "menagerie"
    (nested / ".git").mkdir(parents=True)
    _touch(nested / "vendored.py", "y = 2\n")
    result = graph.build_graph(str(repo))
    paths = {node["path"] for node in result["nodes"]}
    assert "main.py" in paths
    assert "third_party/menagerie/vendored.py" not in paths


# --- call chains ------------------------------------------------------------------


def test_call_chain_walks_resolved_import_edges(tmp_path, monkeypatch):
    repo = _isolated_repo(tmp_path, monkeypatch)
    _touch(repo / "pkg" / "__init__.py", "")
    _touch(repo / "pkg" / "policy.py", "def act(): pass\n")
    _touch(repo / "pkg" / "runner.py", "from pkg import policy\n")
    _touch(repo / "train_main.py", "from pkg import runner\n")

    result = graph.build_graph(str(repo))
    chain_text = "\n".join(result["call_chain"])
    assert "train_main.py" in chain_text
    assert "->" in chain_text  # real edge traversal, not path-substring buckets
    assert "pkg/runner.py" in chain_text

    pack = (repo / ".codex_graph" / "context_pack.md").read_text()
    section = pack.split("## Possible call chain")[1].split("## Suggested tests")[0]
    assert "->" in section


# --- no-task relevance fallback -----------------------------------------------------


def test_no_task_relevance_uses_structural_ranking_with_note(tmp_path, monkeypatch):
    repo = _isolated_repo(tmp_path, monkeypatch)
    _touch(repo / "pkg" / "__init__.py", "")
    _touch(repo / "pkg" / "core_policy.py", "# policy controller env\n")
    _touch(repo / "user_a.py", "from pkg import core_policy\n")
    _touch(repo / "user_b.py", "from pkg import core_policy\n")
    _touch(repo / "lonely_helper.py", "x = 1\n")

    graph.build_graph(str(repo), task="")
    pack = (repo / ".codex_graph" / "context_pack.md").read_text()
    section = pack.split("## Task-relevant files")[1].split("## Possible call chain")[0]
    assert "no task hint" in section
    assert "pkg/core_policy.py" in section  # tagged + imported twice
    assert "lonely_helper.py" not in section  # zero structural signal


# --- stale-pack detection ------------------------------------------------------------


def test_pack_staleness_none_when_fresh(tmp_path, monkeypatch):
    repo = _isolated_repo(tmp_path, monkeypatch)
    _touch(repo / "main.py", "x = 1\n")
    graph.build_graph(str(repo))
    assert graph.pack_staleness(repo) is None


def test_pack_staleness_detects_modified_source(tmp_path, monkeypatch):
    repo = _isolated_repo(tmp_path, monkeypatch)
    _touch(repo / "main.py", "x = 1\n")
    graph.build_graph(str(repo))
    future = time.time() + 60
    os.utime(repo / "main.py", (future, future))
    message = graph.pack_staleness(repo)
    assert message is not None
    assert "STALE" in message


def test_pack_staleness_detects_new_commit(tmp_path, monkeypatch):
    repo = _isolated_repo(tmp_path, monkeypatch)
    _touch(repo / "main.py", "x = 1\n")
    graph.build_graph(str(repo))
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_AUTHOR_DATE": "2099-01-01T00:00:00",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "GIT_COMMITTER_DATE": "2099-01-01T00:00:00",
    }
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "x"], check=True, env=env)
    message = graph.pack_staleness(repo)
    assert message is not None
    assert "commits landed" in message


def test_pack_has_generated_stamp(tmp_path, monkeypatch):
    repo = _isolated_repo(tmp_path, monkeypatch)
    _touch(repo / "main.py", "x = 1\n")
    graph.build_graph(str(repo))
    pack = (repo / ".codex_graph" / "context_pack.md").read_text()
    assert "- Generated: `" in pack


# --- transitive impact ---------------------------------------------------------------


def test_impact_transitive_closure_and_config_refs(tmp_path, monkeypatch, capsys):
    repo = _isolated_repo(tmp_path, monkeypatch)
    _touch(repo / "pkg" / "__init__.py", "")
    _touch(repo / "pkg" / "base.py", "def f(): pass\n")
    _touch(repo / "pkg" / "mid.py", "from pkg import base\n")
    _touch(repo / "run_top.py", "from pkg import mid\nCONFIG = 'settings.yaml'\n")
    _touch(repo / "settings.yaml", "a: 1\n")

    graph.build_graph(str(repo))
    graph.command_impact(SimpleNamespace(repo=str(repo), files=["pkg/base.py"], force_home_scan=False, depth=3))
    payload = json.loads(capsys.readouterr().out)["pkg/base.py"]
    assert payload["direct"] == ["pkg/mid.py"]
    by_path = {item["path"]: item for item in payload["transitive"]}
    assert by_path["pkg/mid.py"]["depth"] == 1
    assert by_path["run_top.py"]["depth"] == 2  # transitive, not just first ring
    assert by_path["run_top.py"]["entrypoint"] is True

    capsys.readouterr()
    graph.command_impact(SimpleNamespace(repo=str(repo), files=["settings.yaml"], force_home_scan=False, depth=3))
    payload = json.loads(capsys.readouterr().out)["settings.yaml"]
    assert payload["config_references"] == ["run_top.py"]


# --- context-budget integration -------------------------------------------------------


def test_context_budget_reports_graph_artifact_token_estimates(tmp_path, monkeypatch, capsys):
    repo = _isolated_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(token_tools, "resolve_repo_root", lambda arg, force_home_scan=False: Path(arg))
    _touch(repo / "main.py", "x = 1\n")
    graph.build_graph(str(repo))
    capsys.readouterr()
    token_tools.command_context_budget(SimpleNamespace(repo=str(repo), max_files=1000, force_home_scan=False))
    out = capsys.readouterr().out
    assert "Graph artifacts" in out
    assert "context_pack.md" in out
    assert "tokens" in out
