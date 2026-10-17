"""Deterministic handoff generation and failure-tolerant external sections."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cdx_agent import handoff


GIT_ENV = dict(
    os.environ,
    GIT_AUTHOR_NAME="t",
    GIT_AUTHOR_EMAIL="t@t",
    GIT_COMMITTER_NAME="t",
    GIT_COMMITTER_EMAIL="t@t",
    GIT_AUTHOR_DATE="2026-10-16T12:00:00",
    GIT_COMMITTER_DATE="2026-10-16T12:00:00",
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, env=GIT_ENV,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout.strip()


def _commit(repo: Path, rel: str, text: str, message: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    _git(repo, "add", rel)
    _git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _commit(root, "README.md", "base\n", "base")
    _git(root, "checkout", "-q", "-b", "feature")
    _commit(root, "reports/nested/eval.md", "# Eval\n\n| x | y |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n| 5 | 6 |\n", "feature report")
    _git(root, "checkout", "-q", "main")
    wt = tmp_path / "feature-wt"
    _git(root, "worktree", "add", "-q", str(wt), "feature")
    (wt / "README.md").write_text("dirty\n")
    status = root / "docs" / "status"
    status.mkdir(parents=True)
    (status / "handoff_notes.md").write_text("# Notes\n## Open\n- keep this\n## Done\n- omit this\n")
    return root


def _stable(monkeypatch):
    monkeypatch.setattr(handoff, "_slurm_lines", lambda *args: ["queued/running: (none)"])
    monkeypatch.setattr(handoff, "_gpu_lines", lambda *args: ["(none)"])
    monkeypatch.setattr(handoff, "_tmux_lines", lambda: ["- session-a"])


def test_handoff_section_order_branch_filter_and_reports(repo, monkeypatch, tmp_path):
    _stable(monkeypatch)
    text = handoff.build_handoff(str(repo), base="main", branches="feat*", since_hours=24,
                                 output_path=str(tmp_path / "out.md"), generated="FIXED")
    assert text.startswith(f"# Handoff {repo.name}\nGenerated: FIXED\n")
    sections = ["## Worktrees", "## Slurm", "## Local GPUs", "## tmux sessions",
                "## Reports", "## Open items", "## Notes (agent-written interpretation)", "## Resume"]
    assert [text.index(section) for section in sections] == sorted(text.index(section) for section in sections)
    assert "- feature:" in text and "- main:" not in text
    assert "dirty 1" in text
    assert "reports/nested/eval.md: Eval" in text
    assert "| 1 | 2 |" in text and "| 3 | 4 |" in text and "| 5 | 6 |" not in text
    assert "- keep this" in text and "- omit this" not in text


def test_handoff_remote_slurm_filters_banners_and_states(monkeypatch):
    calls = []

    def fake_run(command, timeout=30):
        calls.append((command, timeout))
        if "squeue" in command[-1]:
            return "login banner\n123_1 job RUNNING 00:01 n1 none\n"
        return "remote banner\n124 job COMPLETED 00:02 n1\n125 job PENDING 00:01 n1\n"

    monkeypatch.setattr(handoff, "_run_command", fake_run)
    lines = handoff._slurm_lines(False, "cluster", 24)
    assert lines is not None
    joined = "\n".join(lines)
    assert "123_1" in joined and "124" in joined
    assert "login banner" not in joined and "remote banner" not in joined and "125" not in joined
    assert all(call[0][0] == "ssh" and call[1] == 30 for call in calls)


def test_handoff_cap_notes_and_secret_named_files(repo, monkeypatch, tmp_path):
    _stable(monkeypatch)
    notes = tmp_path / "agent_notes.md"
    notes.write_text("n" * 1800)
    secret_report = repo / "reports" / "token_report.md"
    secret_report.parent.mkdir(exist_ok=True)
    secret_report.write_text("# Do not read\n")
    secret_status = repo / "docs" / "status" / "token_todo.md"
    secret_status.write_text("# TODO\n- do not read\n")
    text = handoff.build_handoff(str(repo), output_path=str(tmp_path / "out.md"), notes_path=str(notes),
                                 max_tokens=100, generated="FIXED")
    assert len(text) <= 400
    assert "handoff truncated" in text
    full = handoff.build_handoff(str(repo), output_path=str(tmp_path / "out.md"), notes_path=str(notes),
                                 max_tokens=4000, generated="FIXED")
    assert "Do not read" not in full and "do not read" not in full
    notes_section = full.split("## Notes (agent-written interpretation)\n", 1)[1]
    assert len(notes_section.split("## Resume", 1)[0]) <= 1501


def test_handoff_no_external_sections_and_identical_runs(repo, monkeypatch, tmp_path):
    monkeypatch.setattr(handoff, "_tmux_lines", lambda: ["(none)"])
    out = tmp_path / "handoff.md"
    first = handoff.build_handoff(str(repo), no_slurm=True, no_gpu=True, output_path=str(out), generated="ONE")
    second = handoff.build_handoff(str(repo), no_slurm=True, no_gpu=True, output_path=str(out), generated="TWO")
    assert "## Slurm" not in first and "## Local GPUs" not in first
    assert first.replace("Generated: ONE", "Generated: TIME") == second.replace("Generated: TWO", "Generated: TIME")


def test_handoff_cli_default_out_path(repo, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1] / "src"))
    proc = subprocess.run([sys.executable, "-m", "cdx_agent.handoff", "--repo", str(repo),
                           "--no-slurm", "--no-gpu"], cwd=str(repo), env=os.environ.copy(),
                          text=True, capture_output=True, check=False, timeout=30)
    assert proc.returncode == 0
    lines = proc.stdout.splitlines()
    assert len(lines) == 2 and lines[0].startswith("handoff=") and lines[1].startswith("tokens~=")
    path = Path(lines[0].split("=", 1)[1])
    assert path == repo / "docs/status" / f"{time.strftime('%Y-%m-%d')}_handoff_auto.md"
    assert path.is_file()


def test_handoff_rejects_non_git_repo(tmp_path):
    proc = subprocess.run([sys.executable, "-m", "cdx_agent.handoff", "--repo", str(tmp_path)],
                          env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src")),
                          text=True, capture_output=True, check=False, timeout=30)
    assert proc.returncode == 2
