import hashlib
import os
from pathlib import Path

import pytest
import yaml

from cdx_agent import node, trash


def tree_hash(root):
    out = {}
    for dp, dn, fn in os.walk(root):
        for n in dn + fn:
            p = os.path.join(dp, n)
            out[os.path.relpath(p, root)] = "dir" if os.path.isdir(p) else hashlib.sha256(Path(p).read_bytes()).hexdigest()
    return out


@pytest.fixture
def world(tmp_path):
    ds = tmp_path / "nfs" / "data"
    ds.mkdir(parents=True)
    (ds / "a.txt").write_text("alpha")
    (ds / "sub").mkdir()
    (ds / "sub" / "b.txt").write_text("beta")
    bank = tmp_path / "bank" / "envx"
    bank.mkdir(parents=True)
    (bank / "lib.py").write_text("lib")
    root = tmp_path / "node"
    spec = {"project": "p",
            "items": [{"name": "ds", "source": str(ds), "dest": "data/ds", "sha256": node.tree_digest(str(ds))},
                      {"name": "env", "source": "coldbank:envx", "dest": "env/envx"}],
            "env": {"MYVAR": "$ROOT/x"},
            "outputs": [{"name": "res", "path": "out/res", "dest": str(tmp_path / "nfs" / "res"), "keep": True}]}
    path = tmp_path / "node_env.yaml"
    path.write_text(yaml.safe_dump(spec))
    return tmp_path, root, node.load_spec(str(path)), str(bank)


def test_setup_twice_copies_nothing(world, capsys):
    tp, root, spec, bank = world
    calls = []
    fetch = lambda name, cache: calls.append(name) or bank  # noqa: E731
    assert node.setup(spec, str(root), fetch=fetch) == 0
    assert (root / "data/ds/sub/b.txt").read_text() == "beta"
    assert (root / "env/envx/lib.py").read_text() == "lib"
    sh = (root / "env.sh").read_text()
    for var in ("PIP_CACHE_DIR", "UV_CACHE_DIR", "CONDA_PKGS_DIRS", "HF_HOME", "TORCHINDUCTOR_CACHE_DIR",
                "TRITON_CACHE_DIR", "NUMBA_CACHE_DIR", "XDG_CACHE_HOME", "TMPDIR", "MYVAR"):
        assert f"export {var}=" in sh
    assert f"{root}/x" in sh
    assert calls == ["envx"]
    capsys.readouterr()
    before = tree_hash(root)
    assert node.setup(spec, str(root), fetch=fetch) == 0
    out = capsys.readouterr().out
    assert calls == ["envx"] and "fetched" not in out and out.count("reuse") == 2
    assert tree_hash(root) == before


def test_setup_dry_run_touches_nothing(world, capsys):
    tp, root, spec, bank = world
    before = tree_hash(tp)
    assert node.setup(spec, str(root), dry_run=True) == 0
    out = capsys.readouterr().out
    assert "fetch   ds" in out and "coldbank:envx" in out
    assert tree_hash(tp) == before and not root.exists()


def test_setup_rejects_bad_sha(world):
    tp, root, spec, bank = world
    spec["items"][0]["sha256"] = "0" * 64
    assert node.setup(spec, str(root), fetch=lambda n, c: bank) == 1
    assert not (root / "data/ds").exists()


def test_status(world, capsys):
    tp, root, spec, bank = world
    node.setup(spec, str(root), fetch=lambda n, c: bank)
    capsys.readouterr()
    assert node.status(spec, str(root)) == 0
    out = capsys.readouterr().out
    assert "ds: present" in out and "hash ok" in out and "df " in out
    (root / "data/ds/a.txt").write_text("changed")
    assert node.status(spec, str(root)) == 1


def test_release_copies_back_verified(world, monkeypatch, capsys):
    tp, root, spec, bank = world
    out = root / "out" / "res"
    out.mkdir(parents=True)
    (out / "r.json").write_text("{}")
    removed = []
    monkeypatch.setattr(trash, "rm", lambda paths, dry_run=False, **k: removed.append((list(paths), dry_run)) or 0)
    dest = tp / "nfs" / "res"
    assert node.release(spec, str(root), dry_run=True) == 0
    assert not dest.exists()
    assert node.release(spec, str(root)) == 0
    assert (dest / "r.json").read_text() == "{}"
    man = (dest / "MANIFEST.sha256").read_text()
    assert "r.json" in man and hashlib.sha256(b"{}").hexdigest() in man
    assert removed[-1][0][0] == str(out) and removed[-1][1] is False
    assert "unexpired node-local trash entries" in capsys.readouterr().out
