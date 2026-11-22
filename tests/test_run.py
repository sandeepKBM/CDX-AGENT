"""cdx_agent.run: --plan goldens on fake Slurm text, dry-run, fake-ssh detached run, spec fallback, snapshot."""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from cdx_agent import manifest, nfs, run

FIX = Path(__file__).parent / "fixtures" / "slurm"

SCRIPTS = {
    "ssh": 'echo "$*" >> "$FAKE_LOG"\nwhile [ "$1" = "-o" ]; do shift 2; done\nshift\nexec bash -c "$1"\n',
    "squeue": 'case "$*" in *\'%u|%N\'*) cat "$FIX/squeue_all.txt";; *) cat "$FIX/squeue_$SQ.txt";; esac\n',
    "sinfo": 'case "$*" in *Gres*) cat "$FIX/sinfo_gpu.txt";; *) cat "$FIX/sinfo_cpu.txt";; esac\n',
    "scontrol": 'cat "$FIX/scontrol.txt"\n',
    "sacctmgr": 'cat "$FIX/sacctmgr.txt"\n',
    "srun": ('while [ $# -gt 0 ]; do case "$1" in --jobid=*|--overlap|--gres=*|--mem=*|-t|-w|-J) '
             '[ "$1" = "-t" -o "$1" = "-w" -o "$1" = "-J" ] && shift; shift;; *) break;; esac; done\nexec "$@"\n'),
}


@pytest.fixture
def cluster(tmp_path, monkeypatch):
    """Fake ssh/squeue/sinfo/scontrol/sacctmgr/srun on PATH; ssh runs the remote command locally."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in SCRIPTS.items():
        f = bindir / name
        f.write_text("#!/bin/bash\n" + body)
        f.chmod(f.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FIX", str(FIX))
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "ssh.log"))
    monkeypatch.setenv("USER", "tester")
    monkeypatch.setenv("SQ", "hold")
    monkeypatch.setattr(run.socket, "gethostname", lambda: "testbox")
    monkeypatch.setattr(run, "local_cores_and_load", lambda: (64, 1.0))
    return tmp_path


@pytest.fixture
def repo(cluster, monkeypatch):
    r = cluster / "repo"
    r.mkdir()
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    (r / "a.txt").write_text("tracked\n")
    for args in (["init", "-q"], ["add", "a.txt"], ["commit", "-q", "-m", "x"]):
        subprocess.run(["git", *args], cwd=r, check=True, env=env, capture_output=True)
    monkeypatch.chdir(r)
    return r


def _plan(capsys, *argv):
    assert run.main(["--plan", *argv, "--", "echo", "hi"]) == 0
    return capsys.readouterr().out


def test_plan_hold_on_rlab4_places_a_step(repo, capsys):
    out = _plan(capsys, "--cpus", "48", "--mem", "64G")
    assert out == (FIX / "plan_hold_step.golden").read_text()


def test_plan_no_hold_is_an_ilab_job(repo, capsys, monkeypatch):
    monkeypatch.setenv("SQ", "none")
    out = _plan(capsys, "--cpus", "48", "--mem", "64G")
    assert out == (FIX / "plan_no_hold.golden").read_text()


def test_plan_data_under_data_local_runs_locally(repo, capsys):
    out = _plan(capsys, "--cpus", "8", "--data", "/data/local/x")
    assert out == (FIX / "plan_data_local.golden").read_text()


def test_plan_gpu_hold_without_gpus_falls_to_new_job_rejection(repo, capsys, monkeypatch):
    monkeypatch.setenv("SQ", "none")
    out = _plan(capsys, "--gpus", "1", "--gpu-type", "a4000", "--mem", "8G", "--host-class", "slurm-job")
    assert out.startswith("PLAN class=slurm-job node=")
    assert "rlab4" in out and "rejected slurm-job node=rlab4" in out


def test_parse_time_min():
    assert run.parse_time_min("2h") == 120 and run.parse_time_min("90m") == 90
    assert run.parse_time_min("1:30:00") == 90 and run.parse_time_min("45") == 45
    with pytest.raises(ValueError):
        run.parse_time_min("soon")


def test_dry_run_prints_script_and_ssh_and_runs_nothing(repo, capsys):
    assert run.main(["--dry-run", "--cpus", "4", "--mem", "8G", "--name", "dry", "--", "echo", "hi"]) == 0
    out = capsys.readouterr().out
    assert "export OMP_NUM_THREADS=1" in out and "export NUMBA_NUM_THREADS=1" in out
    assert f"cd {repo}" in out
    cmd = out.split("# command\n", 1)[1].strip()
    assert cmd.startswith("ssh -o BatchMode=yes -o LogLevel=ERROR rlab1 ")
    assert "srun --jobid=700001 --overlap --mem=8G -t 120" in cmd and "node-run --n 0" in cmd
    assert not (repo / ".cdx" / "runs").exists()
    assert "node-run" not in Path(os.environ["FAKE_LOG"]).read_text()   # only read-only Slurm queries were ssh'd


def test_detached_step_in_hold_ends_with_done_json_and_matching_manifest(repo, capsys):
    rc = run.main(["--detach", "--cpus", "2", "--mem", "1G", "--name", "det", "--",
                   "bash", "-c", "echo out-line; exit 3"])
    assert rc == 0
    kv = dict(tok.split("=", 1) for tok in capsys.readouterr().out.split() if "=" in tok)
    run_dir = kv["run_dir"]
    assert run_dir.startswith(str(repo / ".cdx" / "runs"))
    info = run.collect(run_dir, timeout_s=60, poll_s=0.1)
    assert info is not None and info["exit_code"] == 3
    m = manifest.read(run_dir)
    assert m["schema_version"] == 1 and m["exit_code"] == 3 and m["state"] == "failed"
    assert m["placement"]["class"] == "slurm-step" and m["placement"]["slurm_job"] == "700001"
    assert m["command"] == ["bash", "-c", "echo out-line; exit 3"] and m["code"]["head"] and m["finished"]
    assert not any(n.startswith("DONE.json.tmp") for n in os.listdir(run_dir))
    assert json.loads(Path(run_dir, "DONE.json").read_text())["exit_code"] == 3
    assert "out-line" in Path(kv["log"]).read_text()
    index = [json.loads(ln) for ln in (repo / ".cdx" / "jobs.jsonl").read_text().splitlines()]
    assert index[-1]["job_id"] == m["job_id"] and index[-1]["run_dir"] == run_dir
    ssh = Path(os.environ["FAKE_LOG"]).read_text()
    assert "BatchMode=yes" in ssh and "LogLevel=ERROR" in ssh


def test_foreground_local_passes_exit_code_and_scratch_and_snapshot(repo, capfd):
    (repo / ".cdx").mkdir()
    (repo / ".cdx" / "site.json").write_text(json.dumps({
        "local_scratch_root": str(repo.parent / "scratch"), "local_snapshot_root": str(repo.parent / "snaps")}))
    (repo / ".cdx" / "resources.yaml").write_text(
        "profiles:\n  p: {cpus: 2, gpus: 0, host_class: [local], snapshot: true, env: {FOO: bar}}\n"
        "scratch: {MY_CACHE: plan}\n")
    rc = run.main(["--profile", "p", "--", "bash", "-c",
                   'echo "foo=$FOO omp=$OMP_NUM_THREADS cache=$MY_CACHE"; cat a.txt; pwd; exit 5'])
    assert rc == 5
    out = capfd.readouterr().out
    assert "foo=bar omp=1" in out and "tracked" in out
    cache = out.split("cache=")[1].split()[0]
    assert cache.startswith(str(repo.parent / "scratch")) and cache.endswith("/plan") and os.path.isdir(cache)
    snaps = list((repo.parent / "snaps").iterdir())
    assert len(snaps) == 1 and (snaps[0] / "a.txt").exists()
    assert str(snaps[0]) in out                                           # ran from the snapshot, not the repo
    run_dir = next((repo / ".cdx" / "runs").iterdir())
    assert manifest.read(run_dir)["exit_code"] == 5
    # a second run reuses the snapshot dir (same HEAD + diff)
    (snaps[0] / "marker").write_text("x")
    run.main(["--profile", "p", "--", "true"])
    assert (snaps[0] / "marker").exists() and len(list((repo.parent / "snaps").iterdir())) == 1


def test_load_spec_falls_back_to_main_checkout_from_worktree(repo):
    (repo / ".cdx").mkdir()
    (repo / ".cdx" / "resources.yaml").write_text("profiles:\n  x: {cpus: 3}\n")
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    wt = repo.parent / "wt"
    subprocess.run(["git", "worktree", "add", "-q", str(wt)], cwd=repo, check=True, env=env, capture_output=True)
    assert run.load_spec(str(wt)).profiles == {"x": {"cpus": 3}}
    assert run.load_spec(str(repo.parent)).profiles == {}


def test_unknown_profile_and_no_command(repo, capsys):
    assert run.main(["--profile", "nope", "--", "true"]) == 2
    assert run.main(["--cpus", "1"]) == 2


def test_no_placement_exits_2_with_reasons(repo, capsys, monkeypatch):
    monkeypatch.setenv("SQ", "none")
    assert run.main(["--cpus", "48", "--mem", "64G", "--host-class", "slurm-job", "--data", "/data/local/x",
                     "--", "true"]) == 2
    err = capsys.readouterr().err
    assert "PLAN none" in err and "data not readable" in err


def test_wait_visible_is_the_collect_reader(tmp_path):
    d = tmp_path / "r"
    d.mkdir()
    assert run.collect(str(d), timeout_s=0, fallback_rc=4) is None
    assert manifest.read(str(d))["exit_code"] == 4
    assert nfs.wait_visible(str(d / "x"), 0) is False
    assert shutil.which("bash")
