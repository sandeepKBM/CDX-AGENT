"""cdx_agent.brief: deterministic context brief (branch/base, commits, files, open items, reports, rules)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from cdx_agent import brief

SRC = str(Path(__file__).resolve().parents[1] / "src")
GIT_ENV = dict(
    os.environ,
    GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t",
    GIT_AUTHOR_DATE="2026-10-16T12:00:00", GIT_COMMITTER_DATE="2026-10-16T12:00:00",
)

RUNBOOK = """# Runbook
## Setup
ignore me
## Next steps
- step one
- step two
```bash
# not a heading
```
### detail under next
- nested item
## Done
- old
"""

REPORT = """# Eval summary: arm A

| arm | placed |
|---|---|
| A | 5 |
| B | 4 |
| C | 3 |
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, env=GIT_ENV,
                          stdout=subprocess.PIPE, text=True).stdout.strip()


def _commit(repo: Path, rel: str, text: str, msg: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    _git(repo, "add", rel)
    _git(repo, "commit", "-q", "-m", msg)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "README.md", "x\n", "init")
    _git(repo, "checkout", "-q", "-b", "dev")
    _commit(repo, "src/a.py", "a = 1\n", "dev: add a")
    _git(repo, "checkout", "-q", "-b", "feat", "dev")
    _commit(repo, "docs/runbooks/rb.md", RUNBOOK, "runbook next steps")
    _commit(repo, "reports/eval/summary.md", REPORT, "eval report")
    _commit(repo, "docs/status/2026_todo.md", "plain todo line\n", "todo file")
    _commit(repo, "src/b.py", "b = 2\n" * 50, "add b")
    _git(repo, "checkout", "-q", "main")
    return repo


@pytest.fixture
def rules(tmp_path: Path) -> str:
    path = tmp_path / "brief_rules.md"
    path.write_text("# Brief rules\n- no rm -rf\n- terse output\n")
    return str(path)


def test_brief_sections_for_branch(repo, rules):
    text = brief.build_brief("feat", repo=str(repo), rules_path=rules)
    assert text.startswith("# Brief: feat\n")
    # Base comes from the reflog ("Created from dev"), not the main fallback.
    assert "- base: dev (reflog: created from)" in text
    assert "ahead 4, behind 0" in text
    assert "## Commits since base (4)" in text and "add b" in text and "dev: add a" not in text
    assert "## Changed files (4, +" in text
    assert text.index("+50 -0 src/b.py") < text.index("docs/runbooks/rb.md")  # biggest churn first
    assert "docs/runbooks/rb.md > ## Next steps" in text
    assert "- step one" in text and "- nested item" in text
    assert "ignore me" not in text and "- old" not in text
    assert "docs/status/2026_todo.md > (whole file)" in text and "plain todo line" in text
    assert "- reports/eval/summary.md: Eval summary: arm A" in text
    assert "| A | 5 |" in text and "| B | 4 |" in text and "| C | 3 |" not in text
    assert "## Rules\n- no rm -rf\n- terse output\n" in text and "# Brief rules" not in text
    assert "## Output contract" in text and "UNSURE:" in text


def test_brief_worktree_dir_and_explicit_base(repo, rules, tmp_path):
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(wt), "feat")
    (wt / "dirty.txt").write_text("x\n")
    text = brief.build_brief(str(wt), base="main", rules_path=rules)
    assert text.startswith("# Brief: feat\n")
    assert f"- worktree: {wt} (uncommitted: 1 untracked)" in text
    assert "- base: main (--base)" in text and "ahead 5" in text
    assert "dev: add a" in text


def test_brief_falls_back_to_main_without_reflog(repo, rules):
    _git(repo, "branch", "orphan-ish", "feat")  # reflog says "branch: Created from feat"
    _git(repo, "reflog", "expire", "--expire=now", "--all")
    text = brief.build_brief("orphan-ish", repo=str(repo), rules_path=rules)
    assert "- base: main (default)" in text


def test_brief_is_deterministic_and_capped(repo, rules):
    first = brief.build_brief("feat", repo=str(repo), rules_path=rules, max_tokens=250)
    second = brief.build_brief("feat", repo=str(repo), rules_path=rules, max_tokens=250)
    assert first == second
    assert len(first) <= 250 * brief.CHARS_PER_TOKEN
    assert "more lines" in first  # something was truncated, with a marker
    assert "## Output contract" in first and "UNSURE:" in first
    full = brief.build_brief("feat", repo=str(repo), rules_path=rules)
    assert len(full) > len(first) and "more lines" not in full


def test_extract_open_sections_levels_and_fences():
    sections = brief.extract_open_sections(RUNBOOK)
    assert [h for h, _ in sections] == ["## Next steps"]
    body = sections[0][1]
    assert "# not a heading" in body and "### detail under next" in body
    assert brief.extract_open_sections("# Notes\n- x\n") == []
    assert brief.extract_open_sections("- x\n", whole_if_none=True) == [("(whole file)", ["- x"])]


def test_report_headline():
    assert brief.report_headline(REPORT) == ["Eval summary: arm A", "  | arm | placed |", "  | A | 5 |", "  | B | 4 |"]
    assert brief.report_headline("") == ["(untitled)"]


def test_cli_errors_and_output(repo, rules):
    env = dict(os.environ, PYTHONPATH=SRC)
    bad = subprocess.run([sys.executable, "-m", "cdx_agent.brief", "nosuch", "--repo", str(repo)],
                         env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert bad.returncode == 2 and "nosuch" in bad.stderr
    ok = subprocess.run([sys.executable, "-m", "cdx_agent.brief", "feat", "--repo", str(repo),
                         "--rules", rules, "--max-tokens", "1000"],
                        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert ok.returncode == 0 and ok.stdout.startswith("# Brief: feat")
    missing = brief.build_brief("feat", repo=str(repo), rules_path=str(repo / "nope.md"))
    assert "(rules file missing:" in missing
