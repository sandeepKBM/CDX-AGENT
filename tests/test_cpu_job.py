from cdx_agent import cpu_job

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
