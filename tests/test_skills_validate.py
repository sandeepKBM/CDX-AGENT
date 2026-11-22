import json
from pathlib import Path

from cdx_agent import skills, skills_eval


def _skill(root: Path, name: str, desc: str, body: str = "") -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\n{body}\n")
    return d / "SKILL.md"


def test_spec_checks(tmp_path):
    ok = skills.parse_skill_frontmatter(_skill(tmp_path, "good-one", "Does a thing well. Use when a thing is needed."))
    assert ok.is_valid and not ok.warnings
    bad = skills.parse_skill_frontmatter(_skill(tmp_path, "Bad_Name", "Does a thing for the team, use it often."))
    assert any("lowercase" in r for r in bad.reasons)
    assert any("Use when" in w for w in bad.warnings)
    flag = skills.parse_skill_frontmatter(_skill(tmp_path, "flags", "Use when x happens in a run.",
                                                 "run `cdx-agent --hygiene` then `cdx-agent --dg-index`"))
    assert any("--dg-index" in r for r in flag.reasons) and not any("--hygiene" in r for r in flag.reasons)
    long_ = skills.parse_skill_frontmatter(_skill(tmp_path, "long", "Use when long.", "x\n" * 600))
    assert any(">= 500" in r for r in long_.reasons)
    facts = skills.parse_skill_frontmatter(_skill(tmp_path, "facts", "Use when facts.",
                                                  "about 860 GB free, verified 2026-10-16, see /common/nope/x"))
    w = " ".join(facts.warnings)
    assert "GB free" in w and "verified 20" in w and "/common/nope/x" in w


def test_skills_eval_list(tmp_path, capsys):
    p = _skill(tmp_path, "with-evals", "Use when evals.")
    (p.parent / "evals").mkdir()
    (p.parent / "evals" / "evals.json").write_text(json.dumps({"cases": [{"id": "a", "prompt": "p", "pass": "q"}]}))
    _skill(tmp_path, "no-evals", "Use when none.")
    assert skills_eval.main(["--list", "--root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "with-evals: 1 cases" in out and "no evals: no-evals" in out
