import json
import os

from cdx_agent import audit


def test_log_writes_line(tmp_path, monkeypatch):
    monkeypatch.setenv("CDX_AUDIT_DIR", str(tmp_path / "a"))
    monkeypatch.setenv("CDX_SESSION_ID", "s1")
    monkeypatch.setenv("CDX_AGENT_ID", "ag")
    assert audit.log("trashed", "/x/y", tier="b", bytes=3, trash_id="i")
    files = list((tmp_path / "a").glob("delete_*.jsonl"))
    assert len(files) == 1
    rec = json.loads(files[0].read_text().splitlines()[0])
    assert rec["action"] == "trashed" and rec["path"] == "/x/y" and rec["session"] == "s1"
    assert rec["agent"] == "ag" and rec["tier"] == "b" and rec["host"]
    audit.log("denied", "/z")
    assert len(files[0].read_text().splitlines()) == 2


def test_log_never_raises(tmp_path, monkeypatch):
    blocker = tmp_path / "f"
    blocker.write_text("x")
    monkeypatch.setenv("CDX_AUDIT_DIR", str(blocker / "sub"))
    assert audit.log("denied", "/x") is False
    assert os.path.isfile(blocker)
