"""`cdx-agent --graph` (repo_graph_agent) keeps archive/results/data/logs files out of the ranked sections."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

CLI = Path("/common/users/ss5772/codex_tools/repo_graph_agent/repo_graph_agent/cli.py")


def _load():
    if not CLI.is_file():
        pytest.skip("repo_graph_agent not installed")
    spec = importlib.util.spec_from_file_location("rga_cli_under_test", CLI)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_default_and_project_excludes(tmp_path):
    cli = _load()
    for rel, text in {
        "run_real.py": "def main(): pass\n",
        "archive/run_old.py": "def main(): pass\n",
        "pkg/results/run_x.py": "def main(): pass\n",
        "pkg/vendor_cfg.json": "{}",
        "pkg/results_n50.json": "{}",
        "pkg/results/a.json": "{}",
        "scratchy/run_scratch.py": "def main(): pass\n",
    }.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    (tmp_path / ".cdx").mkdir()
    (tmp_path / ".cdx" / "project.json").write_text(json.dumps({"graph_exclude": ["scratchy"]}))
    graph = cli.build_graph(str(tmp_path))
    ep = [e["path"] for e in graph["entrypoints"]]
    assert ep == ["run_real.py"]
    pack = (tmp_path / ".codex_graph" / "context_pack.md").read_text()
    cfg = pack.split("## Important configs")[1].split("##")[0]
    assert "pkg/vendor_cfg.json" in cfg and "results" not in cfg
    assert "archive/run_old.py" in {n["path"] for n in graph["nodes"]}   # still indexed
