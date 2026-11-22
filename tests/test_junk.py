import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from cdx_agent import junk, trash

FREE = lambda p: {"verdict": "free", "evidence": []}  # noqa: E731


def _g(cwd, *a):
    subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                   check=True, capture_output=True)


def tree_hash(root):
    out = {}
    for dp, dn, fn in os.walk(root):
        for n in dn + fn:
            p = os.path.join(dp, n)
            rel = os.path.relpath(p, root)
            if os.path.islink(p):
                out[rel] = "link:" + os.readlink(p)
            elif os.path.isdir(p):
                out[rel] = "dir"
            else:
                out[rel] = hashlib.sha256(Path(p).read_bytes()).hexdigest()
    return out


def old(path, days):
    t = time.time() - days * 86400
    os.utime(path, (t, t))


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    repo = tmp_path / "repo"
    repo.mkdir()
    _g(repo, "init", "-q", "-b", "main")
    (repo / "main.py").write_text("x = 1\n")
    (repo / "NOTES.md").write_text("see session 22222222-aaaa for the data\n")
    _g(repo, "add", ".")
    _g(repo, "commit", "-qm", "init")
    (repo / "pkg" / "__pycache__").mkdir(parents=True)
    (repo / "pkg" / "__pycache__" / "m.pyc").write_bytes(b"pyc")
    for i in range(1, 6):
        f = repo / f"main.py.bak.{i}"
        f.write_text(str(i))
        old(f, 10 - i)  # .bak.5 newest
    _g(repo, "worktree", "add", "-q", "-b", "wtb", str(tmp_path / "wt"))
    slug = "-proj"
    proj = home / ".tmp" / "claude-1" / slug
    for sid in ("11111111-aaaa", "22222222-aaaa"):
        d = proj / sid
        d.mkdir(parents=True)
        (d / "scratch.txt").write_text(sid)
        old(d, 40)
    tr = home / ".claude" / "projects" / slug
    tr.mkdir(parents=True)
    fresh = proj / "33333333-aaaa"
    fresh.mkdir()
    (tr / "33333333-aaaa.jsonl").write_text("{}")
    return tmp_path, repo, home


def test_scan_rows_and_readonly(world):
    tp, repo, home = world
    before = tree_hash(tp)
    res = junk.scan([repo, home / ".tmp"], use_inuse=True, in_use=FREE, repo=repo)
    assert tree_hash(tp) == before
    rows = {r["path"]: r for r in res["rows"]}
    py = rows[str(repo / "pkg" / "__pycache__")]
    assert (py["tier"], py["action"], py["rule"]) == ("a", "delete", "pycache")
    s1 = rows[str(home / ".tmp/claude-1/-proj/11111111-aaaa")]
    assert (s1["tier"], s1["action"]) == ("b", "trash")
    s2 = rows[str(home / ".tmp/claude-1/-proj/22222222-aaaa")]
    assert s2["action"] == "keep" and "referenced by NOTES.md" in s2["reason"]
    assert rows[str(home / ".tmp/claude-1/-proj/33333333-aaaa")]["action"] == "keep"
    baks = sorted(Path(r["path"]).name for r in res["rows"] if r["rule"] == "bak")
    assert baks == ["main.py.bak.1", "main.py.bak.2"]
    wt = rows[str((tp / "wt").resolve())]
    assert (wt["tier"], wt["action"], wt["rule"]) == ("a", "delete", "agent_worktree")
    assert res["totals"]["bak"]["count"] == 2
    assert set(wt) >= {"path", "rule", "bytes", "age_days", "project", "inuse", "tier", "action", "reason"}


def test_cli_json_and_apply_dry_run_change_nothing(world, capsys, monkeypatch):
    tp, repo, home = world
    monkeypatch.chdir(repo)
    assert junk.main(["scan", "--root", str(repo), "--no-inuse", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["rows"] and "totals" in data
    report = tp / "report.json"
    report.write_text(json.dumps(data))
    before = tree_hash(tp)
    monkeypatch.setattr(trash, "_in_use", lambda p: {"verdict": "free", "evidence": []})
    junk.main(["apply", "--from", str(report), "--dry-run"])
    assert tree_hash(tp) == before


def test_apply_only_passes_delete_and_trash(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(trash, "rm", lambda paths, dry_run=False, **k: seen.append((list(paths), dry_run)) or 0)
    rep = tmp_path / "r.json"
    rep.write_text(json.dumps({"rows": [
        {"path": "/a", "action": "delete"}, {"path": "/b", "action": "trash"},
        {"path": "/c", "action": "keep"}, {"path": "/d", "action": "report"}]}))
    assert junk.apply(str(rep), dry_run=True) == 0
    assert seen == [(["/a", "/b"], True)]


def test_apply_removes_clean_worktree_with_git(tmp_path):
    import json as _json
    import subprocess as sp
    from cdx_agent import junk as _junk
    repo = tmp_path / "repo"
    sp.run(["git", "init", "-q", str(repo)], check=True)
    sp.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x"],
           check=True)
    wt = tmp_path / "wt"
    sp.run(["git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(wt)], check=True)
    rep = tmp_path / "r.json"
    rep.write_text(_json.dumps({"rows": [{"path": str(wt), "rule": "agent_worktree", "action": "delete"}]}))
    assert _junk.apply(str(rep), dry_run=True) == 0 and wt.exists()
    assert _junk.apply(str(rep)) == 0
    assert not wt.exists()
    assert "wt" not in sp.run(["git", "-C", str(repo), "worktree", "list"], capture_output=True, text=True).stdout
