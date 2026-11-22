import json

from cdx_agent import site, site_facts


def test_template_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("CDX_SITE_JSON", str(tmp_path / "missing.json"))
    assert site.get("user") is not None
    assert site.get("hosts.gateway") == "rlab1"
    assert site.get("hosts.slurm_prefixes") == ["ilab", "rlab"]
    assert site.get("nope.deeper", default=7) == 7


def test_env_file_wins(monkeypatch, tmp_path):
    path = tmp_path / "site.json"
    path.write_text(json.dumps({"user": "alice", "hosts": {"gateway": "gw"}}))
    monkeypatch.setenv("CDX_SITE_JSON", str(path))
    assert site.get("user") == "alice"
    assert site.get("hosts.gateway") == "gw"
    assert site.get("lock_dir", default="x") == "x"


def test_bad_json_falls_back_to_default(monkeypatch, tmp_path):
    path = tmp_path / "site.json"
    path.write_text("{not json")
    monkeypatch.setenv("CDX_SITE_JSON", str(path))
    assert site.get("user", default="d") == "d"


def test_site_facts_never_fails(monkeypatch, capsys):
    monkeypatch.setattr("cdx_agent.gpu_pick.run_command", lambda cmd: (_ for _ in ()).throw(OSError("no ssh")))
    assert site_facts.main([]) == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) <= 12
    assert out[0].startswith("caps:")
