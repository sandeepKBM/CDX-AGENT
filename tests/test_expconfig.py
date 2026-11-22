import json

import pytest

from cdx_agent import expconfig as ec


def test_defaults_merge_and_overrides(tmp_path):
    (tmp_path / "base.yaml").write_text("a: {b: 1, c: 2}\nx: 1\n")
    (tmp_path / "over.yaml").write_text("a: {c: 3}\n")
    (tmp_path / "main.yaml").write_text("defaults: [base.yaml, over.yaml]\nx: 9\n")
    conf = ec.load(str(tmp_path / "main.yaml"))
    assert conf == {"a": {"b": 1, "c": 3}, "x": 9}
    out = ec.resolve(conf, ["a.b=[1, 2]", "n.m=hello"])
    assert out["a"]["b"] == [1, 2] and out["n"]["m"] == "hello"
    assert conf["a"]["b"] == 1
    assert ec.config_hash(conf) == ec.config_hash(dict(reversed(list(conf.items()))))


def test_sweep():
    assert ec.sweep(["a=1,2", "b=x,y"]) == [["a=1", "b=x"], ["a=1", "b=y"], ["a=2", "b=x"], ["a=2", "b=y"]]
    assert ec.sweep(["s=range(0,6,2)"]) == [["s=0"], ["s=2"], ["s=4"]]
    assert ec.sweep([]) == [[]]


def test_bound_without_tag_rejected():
    with pytest.raises(ValueError, match="box.mu"):
        ec.bounds({"box": {"mu": {"value": 0.5, "source": "x"}}})
    with pytest.raises(ValueError, match="tag"):
        ec.bounds({"mu": {"value": 1, "tag": "GUESS"}})


def test_measured_needs_source_n_stat():
    with pytest.raises(ValueError, match="source path"):
        ec.bounds({"k": {"value": 1, "tag": "MEASURED", "n": 3, "stat": "p95"}})
    with pytest.raises(ValueError, match="'n'"):
        ec.bounds({"k": {"value": 1, "tag": "MEASURED", "source": "a/b.json", "stat": "p95"}})
    ok = ec.bounds({"k": {"value": 1, "tag": "MEASURED", "source": "a/b.json", "n": 3, "stat": "p95"}})
    assert ok["k"]["tag"] == "MEASURED"


def test_new_assumed(tmp_path):
    (tmp_path / ".cdx").mkdir()
    (tmp_path / ".cdx" / "assumed_registry.json").write_text(json.dumps(["box.known"]))
    ledger = tmp_path / "values.jsonl"
    ledger.write_text("")
    conf = {"box": {"known": {"value": 1, "tag": "ASSUMED"}, "fresh": {"value": 2, "tag": "ASSUMED"},
                    "m": {"value": 3, "tag": "USER"}}}
    assert ec.new_assumed(conf, str(tmp_path), str(ledger)) == ["box.fresh"]


def test_on_key_stays_a_string_and_true_is_bool():
    from cdx_agent import expconfig
    d = expconfig._load("b: {value: 1, tag: USER, on: world}\nflag: true\nmode: yes\n")
    assert d["b"]["on"] == "world" and d["flag"] is True and d["mode"] == "yes"
