import subprocess
import sys

import pytest

from cdx_agent import cpu_job

REAL_HOLD_STATE = cpu_job.hold_state


@pytest.fixture(autouse=True)
def _no_hold(monkeypatch):
    """No live Slurm: by default the user has no running jobs (node cap not reached)."""
    monkeypatch.setattr(cpu_job, "hold_state", lambda host, user=None: (1, set()))

SINFO = """ilab1               46/18/0/64
ilab2               18/46/0/64
ilab3               16/80/0/96
rlab3               12/84/0/96
rlab2               80/16/0/96
westeros            0/72/0/72
"""


def test_pick_node_most_idle_within_prefixes():
    assert cpu_job.pick_node(SINFO, ["ilab"], need=8) == "ilab3"
    assert cpu_job.pick_node(SINFO, ["ilab", "rlab"], need=8) == "rlab3"
    assert cpu_job.pick_node(SINFO, ["ilab"], need=90) is None
    assert cpu_job.pick_node("garbage\n", ["ilab"], need=1) is None


def test_build_script_sets_threads_and_cwd():
    s = cpu_job.build_script(["python", "-m", "pytest", "-q", "tests/a b.py"], "/common/users/x y", 4)
    assert s.startswith("cd '/common/users/x y' && export OMP_NUM_THREADS=4")
    assert "TORCH_NUM_THREADS=4" in s and s.endswith("'tests/a b.py'")


SCON = (
    "NodeName=ilab1 CPUTot=64 CPUAlloc=18 RealMemory=524288 AllocMem=524000 State=MIXED\n"
    "NodeName=ilab2 CPUTot=64 CPUAlloc=18 RealMemory=524288 AllocMem=100000 State=MIXED+DRAIN\n"
    "NodeName=ilab3 CPUTot=96 CPUAlloc=16 RealMemory=524288 AllocMem=100000 State=MIXED\n"
)


def test_node_rows_mem_state_cpu():
    rows = cpu_job.node_rows(SINFO, SCON, ["ilab"], 8, 16 * 1024)
    assert [r[0] for r in rows if r[4] is None] == ["ilab3"]
    byname = {r[0]: r for r in rows}
    assert "free mem" in byname["ilab1"][4] and "state" in byname["ilab2"][4]
    assert cpu_job.node_rows(SINFO, SCON, ["ilab"], 90, 1024)[0][4] is not None


def test_main_no_qualifying_node_and_explicit_refuse(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cpu_job, "_fetch", lambda h: (SINFO, SCON))
    base = ["--dry-run", "--cwd", str(tmp_path), "--"]
    assert cpu_job.main(["--mem", "600G"] + base + ["true"]) == 2
    assert "Best 3" in capsys.readouterr().err
    assert cpu_job.main(["--node", "ilab1"] + base + ["true"]) == 2
    assert "refused" in capsys.readouterr().err
    assert cpu_job.main(["--node", "ilab1", "--force"] + base + ["true"]) == 0
    assert cpu_job.main(base + ["true"]) == 0
    cap = capsys.readouterr()
    assert "-w ilab3 " in cap.out and "node=ilab3" in cap.err


def test_profile_mem(tmp_path):
    f = tmp_path / "fp.json"
    f.write_text('{"profiles": {"x": {"steady_gb": 1, "peak_gb": 2.2, "per_proc_gb": 0.5}}}')
    assert cpu_job.profile_mem("x", 1, str(f)) == "3G"
    assert cpu_job.profile_mem("x", 8, str(f)) == "4G"


def test_dry_run_sbatch_and_srun(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(cpu_job, "_fetch", lambda h: (SINFO, SCON))
    assert cpu_job.main(["--node", "ilab3", "--dry-run", "--cwd", str(tmp_path), "--name", "t", "--", "echo", "hi"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("sbatch --parsable -w ilab3 ") and "--wrap" in out and "OMP_NUM_THREADS=8" in out
    assert cpu_job.main(["--node", "ilab3", "--wait", "--timeout", "60", "--dry-run", "--cwd", str(tmp_path), "--", "echo", "hi"]) == 0
    assert capsys.readouterr().out.startswith("timeout 60 srun -w ilab3 ")
    assert cpu_job.main(["--node", "ilab3", "--dry-run"]) == 2


def test_default_prefix_is_ilab_only():
    assert cpu_job.build_parser().parse_args(["--", "x"]).prefix == "ilab"


def _repo(tmp_path, local):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "external").mkdir()
    (repo / "data").symlink_to(local)
    (repo / "ckpt").symlink_to(tmp_path / "shared")
    (repo / "external" / "lib").symlink_to(local)
    return repo


def test_find_unreadable_flags_repo_symlinks_and_command_paths(tmp_path):
    local, shared = tmp_path / "wslocal" / "d", tmp_path / "shared"
    local.mkdir(parents=True)
    shared.mkdir()
    repo = _repo(tmp_path, local)
    found = dict(cpu_job.find_unreadable(["pytest", "tests/x.py"], str(repo), [str(tmp_path / "wslocal")]))
    assert set(found) == {"data", "external/lib"} and found["data"] == str(local)          # ckpt is on shared storage
    assert cpu_job.find_unreadable(["pytest"], str(repo), [str(tmp_path / "elsewhere")]) == []
    assert cpu_job.find_unreadable(["pytest"], str(repo), []) == []
    plain = tmp_path / "plain"
    (plain / ".git").mkdir(parents=True)
    got = cpu_job.find_unreadable(["python", "run.py", "--in", str(local / "f.npy"), "--out=" + str(local / "o")], str(plain),
                                  [str(tmp_path / "wslocal")])
    assert {w for w, _ in got} == {str(local / "f.npy"), str(local / "o")}
    assert cpu_job.find_unreadable(["bash", "-c", f"cat {local}/f"], str(plain), [str(tmp_path / "wslocal")])


def test_main_refuses_and_prints_local_background_command(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cpu_job, "_fetch", lambda h: (SINFO, SCON))
    local = tmp_path / "wslocal" / "d"
    local.mkdir(parents=True)
    repo = _repo(tmp_path, local)
    rc = cpu_job.main(["--node", "ilab3", "--dry-run", "--cwd", str(repo), "--unreadable-prefix", str(tmp_path / "wslocal"),
                       "--name", "suite", "--", "pytest", "-q", "tests/test_x.py"])
    cap = capsys.readouterr()
    assert rc == 2 and cap.out == "" and "data ->" in cap.err
    for part in ("OMP_NUM_THREADS=2", "MKL_NUM_THREADS=2", "OPENBLAS_NUM_THREADS=2", "CUDA_VISIBLE_DEVICES= ", "nice -n 10",
                 "timeout 3600", "pytest -q tests/test_x.py", f"cd {repo}", "nohup", "&"):
        assert part in cap.err
    # explicit override submits anyway
    assert cpu_job.main(["--node", "ilab3", "--dry-run", "--allow-local-data", "--cwd", str(repo),
                         "--unreadable-prefix", str(tmp_path / "wslocal"), "--", "pytest"]) == 0
    assert capsys.readouterr().out.startswith("sbatch ")


def test_default_unreadable_prefix_is_data_local(monkeypatch):
    monkeypatch.delenv("CDX_NODE_UNREADABLE", raising=False)
    assert cpu_job.build_parser().parse_args(["--", "x"]).unreadable_prefix == "/data/local"
    assert cpu_job.DEFAULT_UNREADABLE == ("/data/local",)


def test_per_proc_threads():
    from cdx_agent.cpu_job import per_proc_threads, build_script
    assert per_proc_threads(8, 1) == 8
    assert per_proc_threads(8, 8) == 1
    assert per_proc_threads(8, 3) == 2
    assert per_proc_threads(2, 16) == 1
    assert "OMP_NUM_THREADS=1 " in build_script(["true"], "/tmp", per_proc_threads(8, 8))


def test_node_cap_reached_refuses_and_prints_the_run_line(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cpu_job, "_fetch", lambda h: pytest.fail("no node pick when the cap is reached"))
    monkeypatch.setattr(cpu_job, "hold_state", lambda host, user=None: (1, {"rlab4"}))
    monkeypatch.setattr(cpu_job, "find_unreadable", lambda *a, **k: [])   # tmp_path may sit under /data/local
    rc = cpu_job.main(["--threads", "48", "--mem", "64G", "--time", "2:00:00", "--name", "suite", "--cwd", str(tmp_path),
                       "--", "python", "-m", "pytest", "-q", "tests/a b.py"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "node cap 1 reached (running jobs on rlab4)" in err
    assert ("  cdx-agent --run --cpus 48 --mem 64G --time 2:00:00 --name suite -- python -m pytest -q 'tests/a b.py'"
            in err.splitlines())
    # a node that is the hold's own node is not refused
    monkeypatch.setattr(cpu_job, "_fetch", lambda h: (SINFO, SCON))
    assert cpu_job.main(["--node", "rlab4", "--force", "--dry-run", "--cwd", str(tmp_path), "--", "echo", "hi"]) == 0


def test_hold_state_reads_grptres_and_running_nodes(monkeypatch):
    import subprocess
    monkeypatch.setattr(cpu_job, "read_grptres", lambda user, host: (8, 1))
    monkeypatch.setattr(cpu_job, "run_command", lambda cmd: subprocess.CompletedProcess(
        cmd, 0, "7|hold|rlab4|gres/gpu:a4000:4|RUNNING|None\n8|q|ilab1|N/A|PENDING|Resources\n", ""))
    assert REAL_HOLD_STATE(None, "u") == (1, {"rlab4"})


def test_default_log_dir_is_outside_the_repo(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("CDX_CPU_JOB_LOG_DIR", raising=False)
    repo = tmp_path / "wt"
    repo.mkdir()
    d = cpu_job.log_dir(str(repo))
    assert not d.startswith(str(repo)) and d.endswith("/cpu_jobs/wt")
    assert str(repo) not in cpu_job.local_command(["true"], str(repo), "n").split(" > ")[-1]
    monkeypatch.setenv("CDX_CPU_JOB_LOG_DIR", str(tmp_path / "ovr"))
    assert cpu_job.log_dir(str(repo)) == str(tmp_path / "ovr")
    monkeypatch.setattr(cpu_job, "_fetch", lambda h: (SINFO, SCON))
    assert cpu_job.main(["--dry-run", "--cwd", str(repo), "--", "true"]) == 0
    assert str(tmp_path / "ovr") in capsys.readouterr().out and not (repo / "logs").exists()


# ---- --needs: the node must have the job's libraries / modules / GL backend
SCON_TWO_OK = (
    "NodeName=ilab1 CPUTot=64 CPUAlloc=18 RealMemory=524288 AllocMem=100000 State=MIXED\n"
    "NodeName=ilab3 CPUTot=96 CPUAlloc=16 RealMemory=524288 AllocMem=100000 State=MIXED\n"
)


def test_parse_needs_aliases_dedup_and_errors():
    assert cpu_job.parse_needs(["osmesa,py:numpy", "lib:libEGL.so", "gl:osmesa", "mujoco"]) == [
        "gl:osmesa", "py:numpy", "lib:libEGL.so", "py:mujoco"]
    assert cpu_job.parse_needs([]) == []
    for bad in (["foo:bar"], ["py:"], ["nokind"], ["py:a;rm -rf /"], ["path:/x y"]):
        with pytest.raises(ValueError):
            cpu_job.parse_needs(bad)


def test_infer_needs_and_job_python():
    cmd = ["bash", "-lc", "cd /w && export MUJOCO_GL=osmesa X=1 && /env/bin/python -m pytest -q"]
    assert cpu_job.infer_needs(cmd) == ["gl:osmesa"]
    assert cpu_job.infer_needs(["env", "MUJOCO_GL=disable", "python3", "x.py"]) == []
    assert cpu_job.job_python(cmd) == "/env/bin/python"
    assert cpu_job.job_python(["true"]) == "python3"


def test_probe_script_runs_locally_and_parses():
    needs = ["cmd:bash", "cmd:definitely-not-a-cmd-xyz", "path:/", "path:/no/such/path/xyz", "py:json", "py:no_such_mod_xyz",
             "lib:libc.so"]
    res = cpu_job.probe_local(needs, sys.executable)
    assert {it for it, (ok, _) in res.items() if ok} == {"cmd:bash", "path:/", "py:json", "lib:libc.so"}
    assert "No module named" in res["py:no_such_mod_xyz"][1]
    assert cpu_job.parse_probe("motd line\nCDXNEED\tpy:x\tOK\n", ["py:x", "py:y"]) == {
        "py:x": (True, ""), "py:y": (False, "no probe output")}


def test_main_skips_node_without_needs_and_picks_next(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cpu_job, "_fetch", lambda h: (SINFO, SCON_TWO_OK))
    seen = []

    def fake_probe(node, needs, python, host, ttl=0):
        seen.append((node, tuple(needs), python))
        return {it: (node == "ilab1", "" if node == "ilab1" else "libOSMesa missing") for it in needs}
    monkeypatch.setattr(cpu_job, "probe_node", fake_probe)
    cmd = ["bash", "-lc", "export MUJOCO_GL=osmesa && /env/bin/python run.py"]
    assert cpu_job.main(["--dry-run", "--cwd", str(tmp_path), "--needs", "py:mujoco", "--"] + cmd) == 0
    cap = capsys.readouterr()
    assert "-w ilab1 " in cap.out and "has py:mujoco, gl:osmesa" in cap.err
    assert seen == [("ilab3", ("py:mujoco", "gl:osmesa"), "/env/bin/python"), ("ilab1", ("py:mujoco", "gl:osmesa"), "/env/bin/python")]


def test_main_no_node_has_needs_reports_and_offers_local(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cpu_job, "_fetch", lambda h: (SINFO, SCON_TWO_OK))
    monkeypatch.setattr(cpu_job, "probe_node", lambda node, needs, python, host, ttl=0: {it: (False, "not here") for it in needs})
    monkeypatch.setattr(cpu_job, "probe_local", lambda needs, python: {it: (True, "") for it in needs})
    assert cpu_job.main(["--dry-run", "--cwd", str(tmp_path), "--needs", "osmesa", "--", "true"]) == 2
    err = capsys.readouterr().err
    assert "ilab3: missing gl:osmesa (not here)" in err and "ilab1: missing" in err
    assert "this machine has them" in err and "nohup nice" in err
    assert cpu_job.main(["--dry-run", "--cwd", str(tmp_path), "--needs", "bogus:x", "--", "true"]) == 2


def test_main_explicit_node_refused_without_needs_unless_force(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cpu_job, "_fetch", lambda h: (SINFO, SCON))
    monkeypatch.setattr(cpu_job, "probe_node", lambda node, needs, python, host, ttl=0: {it: (False, "absent") for it in needs})
    base = ["--node", "ilab3", "--dry-run", "--cwd", str(tmp_path), "--needs", "lib:libOSMesa.so"]
    assert cpu_job.main(base + ["--", "true"]) == 2
    assert "missing lib:libOSMesa.so (absent)" in capsys.readouterr().err
    assert cpu_job.main(base + ["--force", "--", "true"]) == 0
    assert "--force" in capsys.readouterr().err


def test_main_no_infer_needs_and_no_needs_means_no_probe(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cpu_job, "_fetch", lambda h: (SINFO, SCON))
    monkeypatch.setattr(cpu_job, "probe_node", lambda *a, **k: pytest.fail("no probe without needs"))
    cmd = ["bash", "-lc", "MUJOCO_GL=osmesa python3 x.py"]
    assert cpu_job.main(["--dry-run", "--cwd", str(tmp_path), "--no-infer-needs", "--"] + cmd) == 0
    assert cpu_job.main(["--dry-run", "--cwd", str(tmp_path), "--", "true"]) == 0


def test_probe_node_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("CDX_NEEDS_CACHE", str(tmp_path / "c.json"))
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="CDXNEED\tpy:a\tOK\nCDXNEED\tpy:b\tMISSING\tNo module named b\n", stderr="")
    monkeypatch.setattr(cpu_job.subprocess, "run", fake_run)
    r1 = cpu_job.probe_node("ilab9", ["py:a", "py:b"], "python3", "gw", ttl=3600)
    assert r1 == {"py:a": (True, ""), "py:b": (False, "No module named b")} and len(calls) == 1
    assert "srun -w ilab9" in calls[0][-1]
    r2 = cpu_job.probe_node("ilab9", ["py:a", "py:b"], "python3", "gw", ttl=3600)
    assert len(calls) == 1 and r2["py:a"][0] is True and r2["py:b"] == (False, "No module named b (cached)")
    cpu_job.probe_node("ilab9", ["py:a"], "python3", "gw", ttl=0)
    assert len(calls) == 2
