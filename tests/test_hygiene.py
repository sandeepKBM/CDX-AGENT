import json
import subprocess
from pathlib import Path

import pytest

from cdx_agent import context_docs, hygiene, launch
from cdx_agent.cli import main as cli_main


def _g(cwd, *a):
    subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _g(r, "init", "-q", "-b", "main")
    (r / "AGENTS.md").write_text("rules v1\n")
    _g(r, "add", "-A")
    _g(r, "commit", "-qm", "init")
    return r


def test_filter_sections_drops_prefix_only():
    t = "intro\n# Keep me\na\n# Served deployments, x\nb\n```\n# not a heading\n```\n# Last\nc\n"
    out = context_docs.filter_global_sections(t, ["Served deployments"])
    assert "Keep me" in out and "Last" in out and "intro" in out
    assert "Served" not in out and "\nb\n" not in out
    assert context_docs.filter_global_sections(t, []) == t


def test_filter_sections_fence_inside_excluded_section():
    t = "# A\nx\n# B\n```\n# C\n```\ny\n# D\nz\n"
    out = context_docs.filter_global_sections(t, ["B"])
    assert out == "# A\nx\n# D\nz\n"


def test_load_exclude_sections(tmp_path):
    assert context_docs.load_exclude_sections(tmp_path) == []
    (tmp_path / ".cdx").mkdir()
    (tmp_path / ".cdx" / "global_rules.json").write_text("{bad")
    assert context_docs.load_exclude_sections(tmp_path) == []
    (tmp_path / ".cdx" / "global_rules.json").write_text(json.dumps({"exclude_sections": ["Served"]}))
    assert context_docs.load_exclude_sections(tmp_path) == ["Served"]


def test_stale_checkout_detects_differing_worktree(repo, tmp_path):
    assert hygiene.stale_checkout_worktrees(repo) == []
    wt = repo / ".claude" / "worktrees" / "feat"
    _g(repo, "worktree", "add", "-q", "-b", "feat", str(wt))
    assert hygiene.stale_checkout_worktrees(repo) == []
    (wt / "AGENTS.md").write_text("rules v2\n")
    _g(wt, "commit", "-qam", "newer")
    names = hygiene.stale_checkout_worktrees(repo)
    assert len(names) == 1 and names[0].startswith("feat")
    assert "feat" in hygiene.stale_warning_line(names)


def test_stale_checkout_ignores_older_worktree_copy(repo, tmp_path):
    import os, time
    wt = repo / ".claude" / "worktrees" / "old"
    _g(repo, "worktree", "add", "-q", "-b", "old", str(wt))
    (wt / "AGENTS.md").write_text("old rules\n")
    past = time.time() - 3600
    os.utime(wt / "AGENTS.md", (past, past))
    (repo / "AGENTS.md").write_text("current rules\n")
    assert hygiene.stale_checkout_worktrees(repo) == []


def test_resolve_worktree(repo):
    wt = repo / ".claude" / "worktrees" / "feat"
    _g(repo, "worktree", "add", "-q", "-b", "feat", str(wt))
    assert hygiene.resolve_worktree(repo, "feat") == wt
    with pytest.raises(ValueError):
        hygiene.resolve_worktree(repo, "nope")


def test_prepare_launch_adds_stale_line_and_filters_sections(repo, tmp_path, capsys):
    from tests.test_launch import _cfg  # type: ignore
    cfg = _cfg(tmp_path)
    (cfg.tools_root / "base").mkdir(parents=True)
    (cfg.tools_root / "base" / "AGENTS.md").write_text("# Keep\nk\n# Served deployments\ndrop\n")
    (repo / ".cdx").mkdir()
    (repo / ".cdx" / "global_rules.json").write_text(json.dumps({"exclude_sections": ["Served deployments"]}))
    wt = repo / ".claude" / "worktrees" / "feat"
    _g(repo, "worktree", "add", "-q", "-b", "feat", str(wt))
    (wt / "AGENTS.md").write_text("other\n")
    res = launch.prepare_launch(cfg, repo, engine="claude", access_mode="safe")
    text = launch.context_docs.target_path(res.plan.runtime.runtime_dir, "claude").read_text()
    assert "Keep" in text and "drop" not in text
    assert "differ from worktree(s): feat" in text
    assert res.stale_worktrees and "WARNING" in capsys.readouterr().err


def test_hygiene_report_flags_and_is_read_only(repo, tmp_path):
    inside = repo / ".claude" / "worktrees" / "merged"
    _g(repo, "worktree", "add", "-q", "-b", "merged", str(inside))  # clean, same commit => merged into main
    outside = tmp_path / "elsewhere"
    _g(repo, "worktree", "add", "-q", "-b", "other", str(outside))
    claude = tmp_path / "claude"
    mem = hygiene.memory_dir_for(repo, claude)
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("x" * 9000)
    before = sorted(p.name for p in repo.rglob("*") if ".git" not in p.parts)
    rep = hygiene.build_report(repo, claude_home=claude)
    assert {w.branch for w, _ in rep.removable} == {"merged", "other"}
    assert [w.branch for w in rep.outside] == ["other"]
    assert rep.memory_over_size and not rep.memory_over_files
    assert "hygiene:" in hygiene.summary_line(rep)
    out = hygiene.format_report(rep)
    assert "worktree remove" in out
    assert before == sorted(p.name for p in repo.rglob("*") if ".git" not in p.parts)
    assert inside.exists() and outside.exists()


def test_hygiene_prunable(repo, tmp_path):
    gone = tmp_path / "gone"
    _g(repo, "worktree", "add", "-q", "-b", "gone", str(gone))
    import shutil
    shutil.rmtree(gone)
    rep = hygiene.build_report(repo, claude_home=tmp_path / "c")
    assert len(rep.prunable) == 1


def test_cli_hygiene_flag(repo, capsys):
    assert cli_main(["--hygiene", "--repo", str(repo)]) == 0
    assert "hygiene report" in capsys.readouterr().out


def test_sanitize_project_path():
    assert hygiene.sanitize_project_path(Path("/a/b.c/d")) == "-a-b-c-d"
