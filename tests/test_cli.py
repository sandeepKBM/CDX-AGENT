from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from cdx_agent.cli import main
from cdx_agent import token_tools


def _write_sample_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "sample-repo"',
                'version = "0.0.1"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (root / "src" / "sample").mkdir(parents=True)
    (root / "src" / "sample" / "__init__.py").write_text("from .main import main\n", encoding="utf-8")
    (root / "src" / "sample" / "main.py").write_text(
        "def main():\n    return 0\n",
        encoding="utf-8",
    )
    (root / "src" / "sample" / "worker.py").write_text(
        "from sample.main import main\n\nresult = main()\n",
        encoding="utf-8",
    )
    (root / "logs").mkdir()
    (root / "logs" / "run.log").write_text("Traceback (most recent call last):\nValueError: boom\n", encoding="utf-8")
    return root


def test_graph_and_relevant_commands(tmp_path, capsys):
    repo = _write_sample_repo(tmp_path / "repo")

    assert main(["--graph", "--repo", str(repo)]) == 0
    graph_path = repo / ".codex_graph" / "repo_graph.json"
    assert graph_path.exists()

    graph_payload = json.loads(graph_path.read_text(encoding="utf-8"))
    entry_paths = [entry["path"] for entry in graph_payload["entrypoints"]]
    assert "src/sample/main.py" in entry_paths

    assert main(["--relevant", "--repo", str(repo), "--task", "update sample main"]) == 0
    relevant_output = capsys.readouterr().out
    assert "src/sample/main.py" in relevant_output

    assert main(["--impact", "--repo", str(repo), "--files", "src/sample/main.py"]) == 0
    impact_output = capsys.readouterr().out
    assert "src/sample/worker.py" in impact_output


def test_context_budget_and_summarize_log(tmp_path, capsys):
    repo = _write_sample_repo(tmp_path / "repo")

    assert main(["--context-budget", "--repo", str(repo)]) == 0
    context_output = capsys.readouterr().out
    assert "Repo:" in context_output
    assert "logs/" in context_output

    log_path = repo / "logs" / "run.log"
    assert token_tools.command_summarize_log(
        SimpleNamespace(input=str(log_path), head_lines=5, tail_lines=5)
    ) == 0
    log_output = capsys.readouterr().out
    assert "Traceback blocks:" in log_output
    assert "ValueError: boom" in log_output


def test_workspace_init_and_help(tmp_path, capsys):
    repo = _write_sample_repo(tmp_path / "repo")

    assert main(["--init-workspace", "--repo", str(repo), "--yes"]) == 0
    assert (repo / ".codex_graph" / "workspace.yaml").exists()

    assert main(["--help"]) == 0
    help_output = capsys.readouterr().out
    assert "CDX-AGENT" in help_output
