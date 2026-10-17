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


def test_dry_run_sbatch_and_srun(capsys, tmp_path):
    assert cpu_job.main(["--node", "ilab3", "--dry-run", "--cwd", str(tmp_path), "--name", "t", "--", "echo", "hi"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("sbatch --parsable -w ilab3 ") and "--wrap" in out and "OMP_NUM_THREADS=8" in out
    assert cpu_job.main(["--node", "ilab3", "--wait", "--timeout", "60", "--dry-run", "--cwd", str(tmp_path), "--", "echo", "hi"]) == 0
    assert capsys.readouterr().out.startswith("timeout 60 srun -w ilab3 ")
    assert cpu_job.main(["--node", "ilab3", "--dry-run"]) == 2
