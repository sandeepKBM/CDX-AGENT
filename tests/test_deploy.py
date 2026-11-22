import hashlib
import json
import subprocess
from pathlib import Path

from cdx_agent import deploy


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _commit(cwd, msg="c"):
    _git(cwd, "add", "-A")
    _git(cwd, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", msg)


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _setup(tmp_path):
    repo = tmp_path / "repo"
    (repo / "tools/bin").mkdir(parents=True)
    (repo / "tools/hooks").mkdir(parents=True)
    (repo / "tools/bin/cdx-agent").write_text("#!/bin/bash\necho v1\n")
    (repo / "tools/bin/cdx-agent").chmod(0o750)
    (repo / "tools/hooks/h.py").write_text("print('h1')\n")
    (repo / ".gitignore").write_text("/site/\n")
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo)
    site = repo / "site"
    (site / "base").mkdir(parents=True)
    (site / "skills/shared/sk").mkdir(parents=True)
    (site / "skills/user/us").mkdir(parents=True)
    (site / "base/AGENTS.md").write_text("rules v1\n")
    (site / "skills/shared/sk/SKILL.md").write_text("sk v1\n")
    (site / "skills/user/us/SKILL.md").write_text("us v1\n")
    _git(site, "init", "-q", "-b", "main")
    _commit(site)
    user_root = tmp_path / "live"
    home = tmp_path / "home"
    return repo, user_root, home


def _args(repo, user_root, home, *extra):
    return ["--repo", str(repo), "--user-root", str(user_root), "--home", str(home), "--skip-tests", *extra]


def test_map_covers_tools_and_site(tmp_path):
    repo, user_root, home = _setup(tmp_path)
    lives = {str(t.live) for t in deploy.build_map(repo, user_root, home)}
    assert str(user_root / "bin/cdx-agent") in lives
    assert str(user_root / "codex_tools/hooks/h.py") in lives
    assert str(user_root / "codex_tools/base/AGENTS.md") in lives
    assert str(user_root / "codex_tools/skills/sk/SKILL.md") in lives
    assert str(home / ".agents/skills/us/SKILL.md") in lives


def test_dry_run_changes_nothing(tmp_path, capsys):
    repo, user_root, home = _setup(tmp_path)
    assert deploy.main(_args(repo, user_root, home, "--dry-run")) == 0
    assert not user_root.exists()
    assert "5 to install" in capsys.readouterr().out


def test_deploy_installs_and_records(tmp_path):
    repo, user_root, home = _setup(tmp_path)
    assert deploy.main(_args(repo, user_root, home)) == 0
    live = user_root / "bin/cdx-agent"
    assert _sha(live) == _sha(repo / "tools/bin/cdx-agent")
    assert live.stat().st_mode & 0o777 == 0o750
    rec = json.loads((user_root / "codex_tools/.deployed.json").read_text())
    assert rec["files"][str(live)] == _sha(live)
    assert (user_root / "codex_tools/.deploy_log.jsonl").read_text().count("\n") == 1
    # second deploy: nothing to do
    assert deploy.main(_args(repo, user_root, home)) == 0
    assert (user_root / "codex_tools/.deploy_log.jsonl").read_text().count("\n") == 1


def test_dirty_tree_refused(tmp_path):
    repo, user_root, home = _setup(tmp_path)
    (repo / "tools/hooks/h.py").write_text("print('dirty')\n")
    assert deploy.main(_args(repo, user_root, home)) == 2
    assert not (user_root / "codex_tools/hooks/h.py").exists()
    assert deploy.main(_args(repo, user_root, home, "--allow-dirty")) == 0
    assert (user_root / "codex_tools/hooks/h.py").read_text() == "print('dirty')\n"


def test_live_drift_refused_unless_force(tmp_path):
    repo, user_root, home = _setup(tmp_path)
    assert deploy.main(_args(repo, user_root, home)) == 0
    hook = user_root / "codex_tools/hooks/h.py"
    hook.write_text("print('edited live')\n")
    (repo / "tools/hooks/h.py").write_text("print('h2')\n")
    _commit(repo, "h2")
    assert deploy.main(_args(repo, user_root, home)) == 2
    assert hook.read_text() == "print('edited live')\n"
    assert deploy.main(_args(repo, user_root, home, "--force")) == 0
    assert hook.read_text() == "print('h2')\n"


def test_first_deploy_accepts_live_equal_to_import(tmp_path):
    """Live files equal to the first committed copy are not drift (the Phase 0 import case)."""
    repo, user_root, home = _setup(tmp_path)
    hook = user_root / "codex_tools/hooks/h.py"
    hook.parent.mkdir(parents=True)
    hook.write_text("print('h1')\n")
    (repo / "tools/hooks/h.py").write_text("print('h2')\n")
    _commit(repo, "h2")
    assert deploy.main(_args(repo, user_root, home)) == 0
    assert hook.read_text() == "print('h2')\n"


def test_rollback_by_rev(tmp_path):
    repo, user_root, home = _setup(tmp_path)
    first = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    (repo / "tools/hooks/h.py").write_text("print('h2')\n")
    _commit(repo, "h2")
    assert deploy.main(_args(repo, user_root, home)) == 0
    assert (user_root / "codex_tools/hooks/h.py").read_text() == "print('h2')\n"
    assert deploy.main(_args(repo, user_root, home, "--rev", first)) == 0
    assert (user_root / "codex_tools/hooks/h.py").read_text() == "print('h1')\n"


def test_live_only_reported_not_deleted(tmp_path, capsys):
    repo, user_root, home = _setup(tmp_path)
    old = user_root / "codex_tools/skills/retired/SKILL.md"
    old.parent.mkdir(parents=True)
    old.write_text("old\n")
    assert deploy.main(_args(repo, user_root, home)) == 0
    assert old.exists()
    assert "live-only" in capsys.readouterr().out
