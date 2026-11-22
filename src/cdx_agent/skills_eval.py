"""cdx-agent --skills-eval --list [--root DIR]...: print the eval cases of each skill (spec 2026-11-20 8.1 item 5).

Each skill may carry `evals/evals.json` = {"skill": name, "cases": [{"id", "prompt", "pass"}], "should_trigger": [...],
"should_not_trigger": [...]}. Running the cases calls a model, so the owner runs them and records the model; this
module only lists them and reports skills without evals.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional, Sequence

DEFAULT_ROOTS = ("/common/users/ss5772/codex_tools/skills", "/common/home/ss5772/.agents/skills")


def load_evals(skill_dir: Path) -> Optional[dict]:
    f = skill_dir / "evals" / "evals.json"
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def rows(roots: Sequence[str]) -> list[tuple[str, Optional[dict]]]:
    out = []
    for r in roots:
        root = Path(r)
        if not root.is_dir():
            continue
        for d in sorted(p for p in root.iterdir() if (p / "SKILL.md").is_file()):
            out.append((d.name, load_evals(d)))
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="cdx-agent --skills-eval")
    ap.add_argument("--list", action="store_true", help="print the eval cases (default)")
    ap.add_argument("--root", action="append", help="skill root (repeatable)")
    a = ap.parse_args(argv)
    missing = []
    for name, ev in rows(a.root or DEFAULT_ROOTS):
        if not ev:
            missing.append(name)
            continue
        cases = ev.get("cases") or []
        print(f"{name}: {len(cases)} cases, {len(ev.get('should_trigger') or [])} should-trigger, "
              f"{len(ev.get('should_not_trigger') or [])} should-not-trigger")
        for c in cases:
            print(f"  {c.get('id')}: {str(c.get('prompt'))[:90]}  -> pass if {str(c.get('pass'))[:80]}")
    if missing:
        print(f"no evals: {', '.join(missing)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
