import json

import pytest

from cdx_agent import values
from cdx_agent.values import main


@pytest.fixture
def led(tmp_path):
    return tmp_path / "v.jsonl"


def test_set_get_history(led):
    r = values.set_value("a.b", 1, tag="USER", reason="r", by="me", path=led)
    assert r["host"] and r["at"] and r["by"] == "me"
    values.set_value("a.b", 2, tag="ASSUMED", reason="r2", path=led)
    assert values.get("a.b", path=led) == 2
    assert [h["value"] for h in values.history("a.b", path=led)] == [1, 2]
    assert values.current(path=led)["a.b"]["value"] == 2
    assert values.row("x.y", path=led) is None


def test_missing_and_corrupt(led):
    assert values.get("a.b", 5, path=led) == 5
    with pytest.raises(KeyError):
        values.get("a.b", path=led)
    led.write_text('garbage\n{"key": "a.b", "value": 3}\n{bad\n')
    assert values.get("a.b", path=led) == 3


def test_validation(led):
    with pytest.raises(ValueError):
        values.set_value("bad", 1, tag="USER", reason="r", path=led)
    with pytest.raises(ValueError):
        values.set_value("a.b", 1, tag="NOPE", reason="r", path=led)
    with pytest.raises(ValueError):
        values.set_value("a.b", 1, tag="USER", reason=" ", path=led)
    with pytest.raises(ValueError):
        values.set_value("a.b", 1, tag="MEASURED", reason="r", path=led)
    values.set_value("a.b", 1, tag="MEASURED", reason="r", source="s", path=led)


def test_env_file_and_by(tmp_path, monkeypatch):
    monkeypatch.setenv("CDX_VALUES_FILE", str(tmp_path / "e.jsonl"))
    monkeypatch.setenv("CDX_AGENT_ID", "agent7")
    r = values.set_value("a.b", 1, tag="USER", reason="r")
    assert r["by"] == "agent7"
    assert values.get("a.b") == 1


def test_seed_file_and_idempotent(led):
    added = values.seed(values.DEFAULT_SEED, path=led)
    assert "trash.ttl_days" in added and len(added) == 9
    assert values.get("output_cap.bytes", path=led) == 8192
    assert values.row("trash.ttl_days", path=led)["host"]
    values.set_value("trash.ttl_days", 9, tag="USER", reason="r", path=led)
    assert values.seed(values.DEFAULT_SEED, path=led) == []
    assert values.get("trash.ttl_days", path=led) == 9


def test_cli(led, monkeypatch, capsys):
    monkeypatch.setenv("CDX_VALUES_FILE", str(led))
    assert main(["get", "a.b"]) == 1
    assert main(["set", "a.b", "12", "--tag", "USER", "--reason", "why", "--unit", "x"]) == 0
    assert main(["set", "a.c", "hello", "--tag", "USER", "--reason", "why"]) == 0
    assert main(["set", "a.d", "1", "--tag", "MEASURED", "--reason", "why"]) == 2
    capsys.readouterr()
    assert main(["get", "a.b"]) == 0
    assert capsys.readouterr().out.strip() == "12"
    assert main(["get", "a.b", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["value"] == 12
    assert values.get("a.c", path=led) == "hello"
    assert main(["list", "--tag", "USER"]) == 0
    assert "a.b=12 x USER" in capsys.readouterr().out
    assert main(["history", "a.b"]) == 0
    assert main(["history", "no.pe"]) == 1
    assert main(["seed"]) == 0
