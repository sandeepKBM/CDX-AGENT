"""cdx_agent.manifest: atomic MANIFEST.json, index line, update at end."""
from __future__ import annotations

import json
import os

from cdx_agent import manifest


def test_write_has_all_keys_null_by_default_and_appends_index(tmp_path):
    run_dir = tmp_path / "repo" / ".cdx" / "runs" / "j1"
    data = manifest.write(str(run_dir), job_id="j1", name="n", command=["echo", "hi"], state="running",
                          placement={"class": "local", "node": "x"})
    on_disk = json.loads((run_dir / "MANIFEST.json").read_text())
    assert on_disk == data and on_disk["schema_version"] == 1
    assert set(manifest.KEYS) <= set(on_disk)
    assert on_disk["exit_code"] is None and on_disk["seeds"] is None and on_disk["command"] == ["echo", "hi"]
    index = tmp_path / "repo" / ".cdx" / "jobs.jsonl"
    lines = [json.loads(ln) for ln in index.read_text().splitlines()]
    assert len(lines) == 1 and lines[0]["job_id"] == "j1" and lines[0]["run_dir"] == str(run_dir)
    manifest.write(str(tmp_path / "repo" / ".cdx" / "runs" / "j2"), job_id="j2")
    assert len(index.read_text().splitlines()) == 2


def test_update_rewrites_atomically_and_leaves_no_tmp(tmp_path):
    run_dir = tmp_path / "r"
    manifest.write(str(run_dir), index="", job_id="a", state="running")
    out = manifest.update(str(run_dir), finished="t", exit_code=0, state="succeeded")
    assert out["job_id"] == "a" and out["exit_code"] == 0 and manifest.read(str(run_dir))["state"] == "succeeded"
    assert sorted(os.listdir(run_dir)) == ["MANIFEST.json"]          # no tmp files, no index (index="")
    assert not (tmp_path / "jobs.jsonl").exists()
    created = manifest.update(str(tmp_path / "new"), state="x")       # absent manifest: created from nulls
    assert created["schema_version"] == 1 and created["state"] == "x" and created["job_id"] is None
