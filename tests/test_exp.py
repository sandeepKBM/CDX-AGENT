import hashlib
import json
import os
import sys

import pytest

from cdx_agent import exp, manifest, stats

ENTRY_SRC = """
import argparse, json, os
p = argparse.ArgumentParser()
p.add_argument("--unit"); p.add_argument("--run-dir"); p.add_argument("--config")
a = p.parse_args()
with open(os.path.join(a.run_dir, "calls.log"), "a") as f:
    f.write(a.unit + "\\n")
print("noise")
print(json.dumps({"unit": a.unit, "success": True, "t": float(a.unit)}))
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    entry = tmp_path / "entry.py"
    entry.write_text(ENTRY_SRC)
    units = tmp_path / "units.txt"
    units.write_text("".join(f"{i}\n" for i in range(10)))
    return tmp_path, str(entry), str(units)


def test_resume_runs_remaining_seven(env):
    tmp, entry, units = env
    rd = tmp / "r1"
    (rd / "units").mkdir(parents=True)
    for i in range(3):
        (rd / "units" / f"{i}.json").write_text(json.dumps({"unit": str(i), "success": True, "t": float(i)}))
    (rd / "PREREG.md").write_text("x")
    assert exp.main(["run", entry, "--units-file", units, "--run-dir", str(rd)]) == 0
    assert sorted((rd / "calls.log").read_text().split()) == sorted(str(i) for i in range(3, 10))
    assert len((rd / "results.jsonl").read_text().splitlines()) == 10
    m = manifest.read(str(rd))
    assert m["world_set_hash"] == hashlib.sha256(open(units, "rb").read()).hexdigest()
    assert m["state"] == "done" and (rd / "DONE.json").exists()


def test_run_requires_prereg_unless_smoke(env, capsys):
    tmp, entry, units = env
    rd = tmp / "r2"
    assert exp.main(["run", entry, "--units-file", units, "--run-dir", str(rd)]) == 2
    assert "exp prereg" in capsys.readouterr().err
    assert exp.main(["run", entry, "--units-file", units, "--run-dir", str(rd), "--smoke"]) == 0
    assert len((rd / "calls.log").read_text().split()) == 2
    assert manifest.read(str(rd))["smoke"] is True


def test_sweep_two_runs(env):
    tmp, entry, units = env
    (tmp / "conf.yaml").write_text("a: {b: 0}\n")
    rc = exp.main(["run", entry, "-c", "conf.yaml", "a.b=1", "-m", "c=1,2", "--units-file", units,
                   "--run-dir", str(tmp / "sw"), "--smoke"])
    assert rc == 0
    assert (tmp / "sw_00" / "config.resolved.yaml").exists() and (tmp / "sw_01" / "config.resolved.yaml").exists()
    assert not (tmp / "sw_02").exists()
    assert "c: 2" in (tmp / "sw_01" / "config.resolved.yaml").read_text()


def test_bad_bound_rejected(env, capsys):
    tmp, entry, units = env
    (tmp / "conf.yaml").write_text("box: {mu: {value: 1, source: s}}\n")
    assert exp.main(["run", entry, "-c", "conf.yaml", "--smoke", "--run-dir", str(tmp / "x")]) == 2
    assert "box.mu" in capsys.readouterr().err


def _mk_run(path, world, rows, perception=None):
    path.mkdir()
    manifest.write(str(path), index="", world_set_hash=world, name="n", code={"head": "h", "diff_sha256": "d"})
    (path / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    if perception is not None:
        (path / "config.resolved.yaml").write_text(f"perception: {perception}\n")


def test_compare_refuses_and_prints(tmp_path, capsys):
    rows = [{"success": True, "t": float(i)} for i in range(20)]
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    _mk_run(a, "h1", rows)
    _mk_run(b, "h2", rows)
    _mk_run(c, "h1", rows, perception="noisy")
    assert exp.main(["compare", str(a), str(b)]) == 2
    assert exp.main(["compare", str(a), str(c)]) == 2
    capsys.readouterr()
    assert exp.main(["compare", str(a), str(a)]) == 0
    out = capsys.readouterr().out
    assert "20/20" in out and f"{stats.clopper_pearson(20, 20)[0]:.4f}" in out and "0.8316" in out
    assert "max over n=20" in out and "<=" not in out


def test_diff(tmp_path, capsys):
    a, b = tmp_path / "a", tmp_path / "b"
    _mk_run(a, "h1", [], perception="x")
    _mk_run(b, "h1", [], perception="y")
    assert exp.main(["diff", str(a), str(b)]) == 0
    assert "perception: 'x' -> 'y'" in capsys.readouterr().out


def test_export_numbered_and_verifies(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CDX_EXPORT_TRUSTED_PREFIX", str(tmp_path))
    a = tmp_path / "a"
    _mk_run(a, "h1", [{"success": True}])
    (a / "logs").mkdir()
    (a / "logs" / "stdout.log").write_text("l")
    dest = tmp_path / "out"
    assert exp.main(["export", str(a), "--to", "out"]) == 0
    names = sorted(os.listdir(dest))
    assert names[0].startswith("01_") and "MANIFEST.sha256" in names
    assert not any("stdout.log" in n for n in names)
    for ln in (dest / "MANIFEST.sha256").read_text().splitlines():
        digest, name = ln.split("  ")
        assert hashlib.sha256((dest / name).read_bytes()).hexdigest() == digest
    assert exp.main(["export", str(a), "--to", ".hid/x"]) == 2
    (tmp_path / ".hidden_real").mkdir()
    (tmp_path / "visible").symlink_to(tmp_path / ".hidden_real")
    assert exp.main(["export", str(a), "--to", "visible/x"]) == 2  # checked on the resolved path



def test_prereg_no_overwrite(tmp_path):
    a = tmp_path / "a"
    _mk_run(a, "h1", [])
    assert exp.main(["prereg", str(a)]) == 0
    text = (a / "PREREG.md").read_text()
    assert "## Pass rule" in text and "h1" in text
    assert exp.main(["prereg", str(a)]) == 2


def test_wrap(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rd = tmp_path / "w"
    rc = exp.main(["wrap", "--name", "t", "--run-dir", str(rd), "--", sys.executable, "-c",
                   "import os,sys;print(os.environ['CDX_RUN_DIR']);sys.exit(3)"])
    assert rc == 3
    m = manifest.read(str(rd))
    assert m["exit_code"] == 3 and m["state"] == "failed" and m["command"][0] == sys.executable
    assert json.loads((rd / "DONE.json").read_text())["exit_code"] == 3
    assert str(rd) in (rd / "logs" / "stdout.log").read_text()
    events = [json.loads(x)["event"] for x in (rd / "events.jsonl").read_text().splitlines()]
    assert events == ["START", "END"]


def test_archive_calls_put(tmp_path, monkeypatch):
    a = tmp_path / "a"
    _mk_run(a, "h1", [])
    seen = {}
    monkeypatch.setattr(exp, "_coldbank_put", lambda s, n, meta: seen.update(s=s, n=n, meta=meta) or 0)
    assert exp.main(["archive", str(a), "--regenerable", "no"]) == 0
    assert seen["s"] == str(a) and any(x.startswith("owner=") for x in seen["meta"])
    assert "regenerable=no" in seen["meta"]


def test_new_assumed_bound_refuses_scored_run_but_not_smoke(env, capsys, monkeypatch):
    tmp, entry, units = env
    monkeypatch.setenv("CDX_VALUES_FILE", str(tmp / "values.jsonl"))
    conf = tmp / "c.yaml"
    conf.write_text("margin: {value: 0.05, tag: ASSUMED, on: world}\n")
    rd = tmp / "r9"
    rd.mkdir()
    (rd / "PREREG.md").write_text("x")
    assert exp.main(["run", entry, "-c", str(conf), "--units-file", units, "--run-dir", str(rd)]) == 2
    assert "margin" in capsys.readouterr().err
    rd2 = tmp / "r10"
    assert exp.main(["run", entry, "-c", str(conf), "--units-file", units, "--run-dir", str(rd2), "--smoke"]) == 0
