import json
import socket

from cdx_agent import suite


def _repo(tmp_path):
    (tmp_path / ".cdx").mkdir()
    (tmp_path / ".cdx" / "test_routes.yaml").write_text(
        'routes:\n  - {glob: "tests/**/test_*cold*.py", host_class: [elsewhere], reason: "reads /data/local"}\n'
        "default: {host_class: [local]}\n")
    tests = tmp_path / "tests"
    (tests / "sub").mkdir(parents=True)
    (tests / "test_ok.py").write_text("def test_ok(): pass\n")
    (tests / "test_bad.py").write_text("def test_bad(): assert 0\n")
    (tests / "test_slow.py").write_text("import time\ndef test_s(): time.sleep(30)\n")
    (tests / "sub" / "test_cold_x.py").write_text("def test_c(): pass  # /data/local/x\n")
    (tests / "test_route.py").write_text("def test_r():\n    open('/data/local/definitely_missing_cdx/x')\n")
    return tmp_path


def test_glob_match():
    assert suite.glob_match("a/b/c/test_x.py", "a/**/test_*.py")
    assert suite.glob_match("a/test_x.py", "a/**/test_*.py")
    assert not suite.glob_match("a/b/test_x.py", "a/*.py")


def test_plan_golden(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(_repo(tmp_path))
    assert suite.main(["--plan", "tests"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].split() == ["file", "host_class", "reason"]
    rows = {line.split()[0]: line.split(maxsplit=2)[1:] for line in out[1:]}
    assert rows["tests/sub/test_cold_x.py"][0] == "elsewhere"
    assert "reads /data/local" in " ".join(rows["tests/sub/test_cold_x.py"])
    assert rows["tests/test_ok.py"][0] == "local"


def test_run_states(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(socket, "gethostname", lambda: "box1")
    out = tmp_path / "out"
    rc = suite.main(["tests", "--jobs", "4", "--timeout-s", "8", "--out", str(out)])
    assert rc == 1
    rows = {r["file"]: r for r in map(json.loads, (out / "results.jsonl").read_text().splitlines())}
    assert rows["tests/test_ok.py"]["state"] == "PASS"
    assert rows["tests/test_bad.py"]["state"] == "FAIL"
    assert rows["tests/test_slow.py"]["state"] == "TIMEOUT"
    assert rows["tests/test_route.py"]["state"] == "ROUTING"
    assert rows["tests/sub/test_cold_x.py"]["state"] == "ROUTED"
    assert "would route to elsewhere" in capsys.readouterr().out
    assert set(rows["tests/test_ok.py"]) == {"file", "host_class", "state", "rc", "seconds", "tail"}


def test_routing_detection_uses_resources(tmp_path, monkeypatch):
    (tmp_path / ".cdx").mkdir()
    (tmp_path / ".cdx" / "resources.yaml").write_text("data:\n  /mnt/cold: {tier: COLD, readable_from: [westeros]}\n")
    monkeypatch.setattr(socket, "gethostname", lambda: "box1")
    prefixes = suite.unreadable_prefixes(tmp_path)
    assert "/mnt/cold" in prefixes and "/data/local" in prefixes
    assert suite.is_routing_failure("FileNotFoundError: [Errno 2] No such file: '/mnt/cold/a.npy'", prefixes)
    assert not suite.is_routing_failure("FileNotFoundError: [Errno 2] No such file: '/tmp/a.npy'", prefixes)
    monkeypatch.setattr(socket, "gethostname", lambda: "westeros.cs.edu")
    assert suite.unreadable_prefixes(tmp_path) == []


def test_scan_prints_proposals(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(_repo(tmp_path))
    assert suite.main(["scan", "tests"]) == 0
    out = capsys.readouterr().out
    assert "tests/sub/test_cold_x.py" in out and "tests/test_ok.py" not in out
