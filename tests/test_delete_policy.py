import json
import os
import subprocess
from pathlib import Path

import pytest

from cdx_agent import delete_policy as dp


def _g(cwd, *a):
    subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True, capture_output=True)


FREE = lambda p: {"verdict": "free", "evidence": []}  # noqa: E731


@pytest.fixture
def world(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    proj = tmp_path / "proj"
    proj.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    trash = tmp_path / "trash"
    trash.mkdir()
    pol = dp.load_policy(dp.PACKAGE_TEMPLATE)
    ctx = dp.Ctx(session_scratch=scratch, home=home, project_roots=[proj], trash_roots=[trash],
                 uid=os.getuid(), in_use=FREE, verified_copy=False, policy=pol)
    return ctx, tmp_path


def test_template_loads_and_expands(world, monkeypatch):
    monkeypatch.delenv("CDX_DELETE_POLICY", raising=False)
    pol = dp.load_policy(dp.PACKAGE_TEMPLATE)
    assert pol["version"] == 1 and pol["warnings"] == []
    caches = [e for e in pol["allow"] if e["id"] == "caches"][0]
    assert str(Path.home() / ".cache/pip") in caches["prefix"]


def test_allow_names_and_scratch(world):
    ctx, tp = world
    d = tp / "proj" / "pkg" / "__pycache__"
    d.mkdir(parents=True)
    assert dp.classify(d, ctx).tier == "a"
    f = ctx.session_scratch / "x.txt"
    f.write_text("x")
    assert dp.classify(f, ctx).tier == "a"


def test_default_b_and_protect_c(world):
    ctx, tp = world
    f = tp / "stuff" / "notes.txt"
    f.parent.mkdir()
    f.write_text("n")
    assert dp.classify(f, ctx).tier == "b"
    ck = tp / "stuff" / "m.pt"
    ck.write_text("w")
    dec = dp.classify(ck, ctx)
    assert (dec.tier, dec.critical) == ("c", True)
    dc = dp.classify(tp / "stuff", ctx)  # contains a protected file
    assert (dc.tier, dc.critical) == ("c", True)
    ctx.verified_copy = True
    dec = dp.classify(ck, ctx)
    assert (dec.tier, dec.critical) == ("b", True)


def test_glob_double_star(world):
    ctx, tp = world
    d = tp / "x" / "checkpoints" / "y"
    d.mkdir(parents=True)
    assert dp.classify(d, ctx).tier == "c"
    assert dp.glob_to_regex("**/checkpoints/**").match("/x/checkpoints/y")


def test_hard_rules(world):
    ctx, tp = world
    assert dp.classify(ctx.home, ctx.__class__(**{**ctx.__dict__})).rule_id == "home"
    assert dp.classify(tp, ctx).tier == "c"  # ancestor of home
    assert dp.classify(tp / "proj", ctx).rule_id == "project_root"
    t = tp / "trash" / "a" / "__pycache__"
    t.mkdir(parents=True)
    assert dp.classify(t, ctx).rule_id == "trash_root"
    g = tp / "proj" / ".git" / "__pycache__"
    g.mkdir(parents=True)
    assert dp.classify(g, ctx).rule_id == "git_dir"
    p = tp / "stuff" / "__pycache__"
    p.mkdir(parents=True)
    ctx.in_use = lambda x: {"verdict": "unknown", "evidence": ["ssh failed"]}
    assert dp.classify(p, ctx).rule_id == "in_use"
    ctx.in_use = FREE
    ctx.uid = os.getuid() + 1
    assert dp.classify(p, ctx).rule_id == "foreign_owner"


def test_mount_point(world, monkeypatch):
    ctx, tp = world
    d = tp / "mnt"
    d.mkdir()
    monkeypatch.setattr(os.path, "ismount", lambda p: str(p) == str(d))
    assert dp.classify(d, ctx).rule_id == "mount_point"


def test_merged_worktree_allowed_diverged_not(world):
    ctx, tp = world
    repo = tp / "repo"
    repo.mkdir()
    _g(repo, "init", "-q", "-b", "main")
    (repo / "a").write_text("a")
    _g(repo, "add", "-A")
    _g(repo, "commit", "-qm", "i")
    ctx.project_roots = [repo]
    ok = tp / "wt_ok"
    _g(repo, "worktree", "add", "-q", "-b", "ok", str(ok))
    div = tp / "wt_div"
    _g(repo, "worktree", "add", "-q", "-b", "div", str(div))
    (div / "b").write_text("b")
    _g(div, "add", "-A")
    _g(div, "commit", "-qm", "x")
    assert dp.classify(ok, ctx).tier == "a"
    assert dp.classify(div, ctx).tier == "b"
    (ok / "dirty").write_text("d")
    assert dp.classify(ok, ctx).tier == "b"
    assert dp.classify(repo, ctx).rule_id == "project_root"


def test_project_policy_merge(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _g(repo, "init", "-q", "-b", "main")
    (repo / ".cdx").mkdir()
    (repo / ".cdx" / "delete_policy.json").write_text(json.dumps({
        "allow": [{"id": "ok", "prefix": [str(repo / "build"), "/elsewhere"]},
                  {"id": "bad", "prefix": "/etc"}, {"id": "names", "names": ["x"]}],
        "protect": [{"id": "p", "glob": "**/keep/**"}]}))
    base = tmp_path / "base.json"
    base.write_text(json.dumps({"version": 1, "allow": [], "protect": []}))
    wt = tmp_path / "wt"
    _g(repo, "add", "-A")
    _g(repo, "commit", "-qm", "i")
    _g(repo, "worktree", "add", "-q", "-b", "w", str(wt))
    pol = dp.load_policy(base, wt)  # worktree sees the main checkout's file
    assert [e["id"] for e in pol["allow"]] == ["ok"]
    assert pol["allow"][0]["prefix"] == [str(repo / "build")]
    assert len(pol["warnings"]) == 2
    assert pol["protect"][0]["id"] == "p"


def test_default_ctx(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _g(repo, "init", "-q", "-b", "main")
    (repo / "a").write_text("a")
    _g(repo, "add", "-A")
    _g(repo, "commit", "-qm", "i")
    wt = tmp_path / "wt"
    _g(repo, "worktree", "add", "-q", "-b", "w", str(wt))
    monkeypatch.setenv("CDX_SESSION_SCRATCH", str(tmp_path / "sc"))
    monkeypatch.setenv("CDX_TRASH_ROOTS", json.dumps([str(tmp_path / "tr")]))
    ctx = dp.default_ctx(wt, verified_copy=True)
    assert ctx.session_scratch == tmp_path / "sc" and ctx.verified_copy
    assert ctx.trash_roots == [tmp_path / "tr"]
    assert {r.resolve() for r in ctx.project_roots} == {wt.resolve(), repo.resolve()}
    assert ctx.uid == os.getuid() and ctx.home == Path.home()
    from cdx_agent import inuse
    assert ctx.in_use is inuse.in_use and ctx.policy["allow"]
    monkeypatch.delenv("CDX_TRASH_ROOTS")
    monkeypatch.delenv("CDX_SESSION_SCRATCH")
    ctx2 = dp.default_ctx(wt, in_use=FREE, session_scratch=Path("/s"))
    assert ctx2.session_scratch == Path("/s") and ctx2.in_use is FREE
    assert Path(dp.DEFAULT_TRASH_ROOTS[0]) in ctx2.trash_roots
