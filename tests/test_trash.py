import hashlib
import json
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from cdx_agent import trash


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
                with open(p, "rb") as fh:
                    out[rel] = hashlib.sha256(fh.read()).hexdigest()
    return out


@pytest.fixture
def env(tmp_path, monkeypatch):
    staging = tmp_path / "trash_staging"
    dest = tmp_path / "dest"
    dest.mkdir()
    monkeypatch.setenv("CDX_TRASH_ROOTS", json.dumps([str(staging), str(dest)]))
    monkeypatch.setenv("CDX_TRASH_INDEX", str(tmp_path / "idx" / "index.jsonl"))
    monkeypatch.setenv("CDX_AUDIT_DIR", str(tmp_path / "audit"))
    state = {"tiers": {}, "use": {}, "values": {"trash.ttl_days": 7, "trash.nfs_staging_cap_gb": 10}}

    def classify(path, ctx):
        t, rule, crit = state["tiers"].get(os.path.basename(str(path)), ("b", "other", False))
        return SimpleNamespace(tier=t, rule_id=rule, reason="", critical=crit)

    dp = types.ModuleType("cdx_agent.delete_policy")
    dp.classify = classify
    dp.default_ctx = lambda repo=None, verified_copy=False: object()
    iu = types.ModuleType("cdx_agent.inuse")
    iu.in_use = lambda p: state["use"].get(os.path.basename(str(p)), {"verdict": "free", "evidence": []})
    vals = types.ModuleType("cdx_agent.values")
    vals.get = lambda key, default=None: state["values"].get(key, default)
    for name, mod in (("delete_policy", dp), ("inuse", iu), ("values", vals)):
        monkeypatch.setitem(sys.modules, f"cdx_agent.{name}", mod)
    state["root"] = tmp_path
    state["staging"], state["dest"] = staging, dest
    return state


def make_tree(base):
    (base / "sub").mkdir(parents=True)
    (base / "a.txt").write_text("hello")
    (base / "sub" / "b.bin").write_bytes(b"\x00\x01" * 100)
    os.symlink("a.txt", base / "link")
    os.symlink("/nonexistent/target", base / "sub" / "dangling")


def audit_records(env):
    out = []
    for f in (env["root"] / "audit").glob("delete_*.jsonl"):
        out += [json.loads(ln) for ln in f.read_text().splitlines()]
    return out


def test_trash_restore_identical(env):
    work = env["root"] / "work"
    work.mkdir()
    proj = work / "proj"
    make_tree(proj)
    before = tree_hash(proj)
    assert trash.rm([str(proj)]) == 0
    assert not proj.exists()
    state = trash.index_state()
    assert len(state) == 1
    eid, e = next(iter(state.items()))
    ed = e["entry_dir"]
    assert os.path.isfile(os.path.join(ed, "entry.json")) and os.path.isfile(os.path.join(ed, "MANIFEST.sha256"))
    assert e["files"] == 4 and e["filesystem"] == str(env["staging"])
    assert trash.restore(eid) == 0
    assert tree_hash(proj) == before
    assert not trash.index_state()
    assert [r["action"] for r in audit_records(env)] == ["trashed", "restored"]


def test_restore_refuses_existing_and_mismatch(env):
    work = env["root"] / "w"
    work.mkdir()
    f = work / "f.txt"
    f.write_text("one")
    trash.rm([str(f)])
    eid, e = next(iter(trash.index_state().items()))
    f.write_text("new")
    assert trash.restore(eid) == 2
    f.unlink()
    with open(os.path.join(e["entry_dir"], "payload", "f.txt"), "w") as fh:
        fh.write("tampered")
    assert trash.restore(eid) == 2
    assert not f.exists()


def test_tier_a_deletes_directly(env):
    d = env["root"] / "w" / "__pycache__"
    d.mkdir(parents=True)
    (d / "x.pyc").write_text("x")
    env["tiers"]["__pycache__"] = ("a", "pycache", False)
    assert trash.rm([str(d)]) == 0
    assert not d.exists() and not trash.index_state()
    assert audit_records(env)[0]["action"] == "deleted"


def test_tier_c_refused(env, capsys):
    f = env["root"] / "w" / "m.pt"
    f.parent.mkdir()
    f.write_text("x")
    env["tiers"]["m.pt"] = ("c", "ckpt", True)
    assert trash.rm([str(f)]) == 2
    assert f.exists()
    assert "protected (ckpt): only the user may delete this" in capsys.readouterr().out
    rec = audit_records(env)[0]
    assert rec["action"] == "denied" and rec["rule_id"] == "ckpt"


def test_in_use_refused(env):
    f = env["root"] / "w" / "busy"
    f.parent.mkdir()
    f.write_text("x")
    env["use"]["busy"] = {"verdict": "unknown", "evidence": ["ssh down"]}
    assert trash.rm([str(f)]) == 2
    assert f.exists()
    rec = audit_records(env)[0]
    assert rec["action"] == "denied" and rec["in_use"]["verdict"] == "unknown"


def test_dry_run_changes_nothing(env, capsys):
    work = env["root"] / "w"
    make_tree(work / "p")
    before = tree_hash(env["root"])
    assert trash.rm([str(work / "p")], dry_run=True) == 0
    assert tree_hash(env["root"]) == before
    assert not env["staging"].exists()
    assert "action=trash" in capsys.readouterr().out
    assert not audit_records(env)


def test_from_report(env):
    work = env["root"] / "w"
    work.mkdir()
    (work / "x").write_text("1")
    rep = env["root"] / "rep.json"
    rep.write_text(json.dumps([{"path": str(work / "x")}]))
    assert trash.from_report(str(rep)) == 0
    assert not (work / "x").exists()


def test_no_root_on_fs(env, monkeypatch, capsys):
    monkeypatch.setenv("CDX_TRASH_ROOTS", json.dumps(["/nonexistent_zz/a/b"]))
    f = env["root"] / "f"
    f.write_text("x")
    assert trash.rm([str(f)]) == 2
    assert f.exists()
    assert "no trash root on this filesystem; add one to CDX_TRASH_ROOTS" in capsys.readouterr().out


def test_staging_cap(env, capsys):
    env["values"]["trash.nfs_staging_cap_gb"] = 100 / 1024 ** 3
    f = env["root"] / "f"
    f.write_bytes(b"x" * 200)
    assert trash.rm([str(f)]) == 2
    assert f.exists() and "staging cap" in capsys.readouterr().out
    env["values"]["trash.nfs_staging_cap_gb"] = 10
    assert trash.rm([str(f)]) == 0


def test_purge_ttl_and_critical(env):
    w = env["root"] / "w"
    w.mkdir()
    base = datetime(2026, 11, 20, tzinfo=timezone.utc)
    env["tiers"]["crit"] = ("b", "r", True)
    # deleted_at = base - age days, expires_at = deleted_at + 7d; purge runs at `base`
    for name, age in (("new", 6), ("old", 8), ("crit", 8)):
        (w / name).write_text(name)
        trash.rm([str(w / name)], now=base - timedelta(days=age))
    # 6-day-old: expires in 1 day; 8-day-old: expired 1 day ago
    assert len(trash.purge(now=base)) == 1
    names = sorted(os.path.basename(e["original_path"]) for e in trash.index_state().values())
    assert names == ["crit", "new"]
    assert len(trash.purge(now=base)) == 0
    assert len(trash.purge(now=base, include_critical=True)) == 1
    assert [os.path.basename(e["original_path"]) for e in trash.index_state().values()] == ["new"]
    assert [r["action"] for r in audit_records(env)].count("purged") == 2


def test_purge_ignores_entry_outside_roots(env, monkeypatch):
    w = env["root"] / "w"
    w.mkdir()
    (w / "f").write_text("x")
    base = datetime(2026, 11, 20, tzinfo=timezone.utc)
    trash.rm([str(w / "f")], now=base - timedelta(days=20))
    _, e = next(iter(trash.index_state().items()))
    monkeypatch.setenv("CDX_TRASH_ROOTS", json.dumps([str(env["root"] / "elsewhere")]))
    assert trash.purge(now=base) == []
    assert os.path.isdir(e["entry_dir"])


def test_drain_copies_verifies_clears(env):
    w = env["root"] / "w"
    make_tree(w / "p")
    before = tree_hash(w / "p")
    trash.rm([str(w / "p")])
    eid, e = next(iter(trash.index_state().items()))
    old_dir = e["entry_dir"]
    assert trash.drain(str(env["dest"])) == {"drained": [eid], "failed": []}
    assert not os.path.exists(old_dir)
    e2 = trash.index_state()[eid]
    assert e2["entry_dir"].startswith(str(env["dest"])) and e2["filesystem"] == str(env["dest"])
    assert trash.verify_manifest(e2["entry_dir"]) == []
    assert trash.restore(eid) == 0
    assert tree_hash(w / "p") == before
    assert "drained" in [r["action"] for r in audit_records(env)]


def test_drain_dest_absent(env):
    assert trash.drain(str(env["root"] / "nope")) == {"drained": [], "failed": []}


def test_status_and_cli(env, capsys):
    w = env["root"] / "w"
    w.mkdir()
    (w / "f").write_text("abc")
    assert trash.main(["rm", str(w / "f")]) == 0
    capsys.readouterr()
    st = trash.status()
    assert st["roots"][str(env["staging"])]["entries"] == 1 and st["critical"] == 0
    assert trash.main(["list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["bytes"] == 3
