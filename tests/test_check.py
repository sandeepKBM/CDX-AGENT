import json
import subprocess

from cdx_agent import check


def _fixture(tmp_path):
    (tmp_path / ".cdx").mkdir()
    (tmp_path / ".cdx" / "project.json").write_text(json.dumps(
        {"entry": "run.py", "ledger": "docs/LEDGER.md", "docs_index": "docs/README.md"}))
    (tmp_path / ".cdx" / "test_routes.yaml").write_text(
        'routes:\n  - {glob: "tests/test_a.py", host_class: [local]}\n  - {glob: "nope/**/x.py", host_class: [local]}\n')
    (tmp_path / ".cdx" / "long_suites.txt").write_text("tests/test_a.py\ntests/test_missing.py local-only\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("x\n")
    return tmp_path


def test_check_repo_flags_bad_route_and_missing_file(tmp_path):
    root = _fixture(tmp_path)
    findings = check.check_repo(root)
    assert any("nope/**/x.py" in f for f in findings)
    assert any("tests/test_missing.py" in f for f in findings)
    assert len(findings) == 2
    assert check.main(["repo", "--repo", str(root)]) == 1


def test_check_repo_clean_and_parse_error(tmp_path):
    (tmp_path / ".cdx").mkdir()
    (tmp_path / ".cdx" / "project.json").write_text("{bad")
    assert any("project.json" in f for f in check.check_repo(tmp_path))
    (tmp_path / ".cdx" / "project.json").write_text("{}")
    assert check.main(["repo", "--repo", str(tmp_path)]) == 0


def test_check_docs(tmp_path):
    root = _fixture(tmp_path)
    docs = root / "docs"
    docs.mkdir()
    (docs / "README.md").write_text("| file | what |\n|---|---|\n| `A.md` | a |\n| `GONE.md` | g |\n")
    (docs / "A.md").write_text("see `tests/test_a.py` and tests/test_a.py:1 and tests/test_a.py:99 and `src/none.py` "
                               "and nothing.py:3\n")
    (docs / "B.md").write_text("orphan\n")
    (docs / "LEDGER.md").write_text("| 11-20 | ruled |\n| 2026-11-20 | ok |\n")
    (root / "AGENTS.md").write_text("`docs/A.md` fine, `docs/missing.md` not\n")
    findings = "\n".join(check.check_docs(root))
    assert "lists a file that does not exist: GONE.md" in findings
    assert "docs/B.md is on disk but not in the index" in findings
    assert "line out of range" in findings and "tests/test_a.py:99" in findings
    assert "nothing.py:3" in findings and "src/none.py" in findings
    assert "docs/missing.md" in findings
    assert "LEDGER.md:1: ledger row date has no year" in findings
    assert "LEDGER.md:2" not in findings
    assert "does not resolve: tests/test_a.py:1" not in findings


def test_check_report(tmp_path):
    report = tmp_path / "r.md"
    report.write_text(
        "max latency <= 5 ms\n"
        "p95 ≤ 3\n"
        "success 85% overall\n"
        "success 17/20 = 85%\n"
        "rate 85% (n=20)\n"
        "\n"
        "| method | score |\n|---|---|\n| a | 1 |\n"
        "\n"
        "| baseline | box | score |\n|---|---|---|\n| a | b | 1 |\n"
    )
    findings = check.check_report(report)
    lines = sorted(int(f.split(":")[1]) for f in findings)
    assert lines == [1, 2, 3, 7]


def test_check_refs(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "a.txt").write_text("trash TTL is 3 days\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "a.txt"], check=True)
    assert check.check_refs(tmp_path, "TTL is 3") == ["a.txt:1:trash TTL is 3 days"]
    assert check.main(["refs", "TTL is 3", "--repo", str(tmp_path)]) == 1
    assert check.main(["refs", "zzz", "--repo", str(tmp_path)]) == 0
