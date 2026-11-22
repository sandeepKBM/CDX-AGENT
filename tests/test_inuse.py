import os
import stat

import pytest

from cdx_agent import inuse


def _exe(path, body):
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def env(tmp_path, monkeypatch):
    binp = tmp_path / "bin"
    binp.mkdir()
    monkeypatch.setenv("PATH", f"{binp}:/usr/bin:/bin")
    monkeypatch.setenv("USER", "tester")
    monkeypatch.delenv("CDX_WORKSTATIONS", raising=False)
    target = tmp_path / "data" / "proj"
    target.mkdir(parents=True)
    proc = tmp_path / "proc"
    proc.mkdir()
    mounts = tmp_path / "mounts"
    mounts.write_text(f"srv:/x {tmp_path} nfs4 rw 0 0\n")
    return binp, target, proc, mounts


def _fake_pid(proc, pid, cwd=None):
    d = proc / str(pid)
    d.mkdir()
    (d / "comm").write_text("python\n")
    (d / "cmdline").write_bytes(b"python\0x.py\0")
    if cwd:
        os.symlink(str(cwd), d / "cwd")


def test_free(env):
    binp, target, proc, mounts = env
    _exe(binp / "squeue", "exit 0\n")
    _exe(binp / "ssh", "exit 0\n")
    _fake_pid(proc, 11, cwd="/")
    r = inuse.in_use(target, ["ws1"], proc_root=str(proc), mounts_file=str(mounts))
    assert r == {"verdict": "free", "evidence": []}


def test_proc_cwd_in_use(env):
    binp, target, proc, mounts = env
    _fake_pid(proc, 12, cwd=target / "sub")
    (target / "sub").mkdir()
    r = inuse.in_use(target, proc_root=str(proc), local_only=True)
    assert r["verdict"] == "in_use" and "pid 12" in r["evidence"][0]


def test_proc_cmdline_in_use(env):
    _, target, proc, _ = env
    d = proc / "13"
    d.mkdir()
    (d / "cmdline").write_bytes(b"python\0" + str(target / "a.py").encode() + b"\0")
    assert inuse.in_use(target, proc_root=str(proc), local_only=True)["verdict"] == "in_use"


def test_local_only_skips_squeue_and_ssh(env):
    binp, target, proc, mounts = env
    _exe(binp / "squeue", "echo SHOULD-NOT-RUN >&2; exit 1\n")
    r = inuse.in_use(target, proc_root=str(proc), mounts_file=str(mounts), local_only=True)
    assert r == {"verdict": "free", "evidence": []}


def test_squeue_workdir_in_use(env):
    binp, target, proc, mounts = env
    _exe(binp / "squeue", f"echo '77|rlab1|{target}/run|python train.py'\n")
    r = inuse.in_use(target, [], proc_root=str(proc), mounts_file=str(mounts))
    assert r["verdict"] == "in_use" and "job 77" in r["evidence"][0]


def test_ssh_failure_is_unknown_on_nfs(env):
    binp, target, proc, mounts = env
    _exe(binp / "squeue", "exit 0\n")
    _exe(binp / "ssh", "exit 255\n")
    r = inuse.in_use(target, ["ws1"], proc_root=str(proc), mounts_file=str(mounts))
    assert r["verdict"] == "unknown" and "ws1" in r["evidence"][0]


def test_ssh_not_used_off_network(env, tmp_path):
    binp, target, proc, _ = env
    _exe(binp / "squeue", "exit 0\n")
    _exe(binp / "ssh", "exit 255\n")
    local = tmp_path / "m2"
    local.write_text(f"/dev/sda1 {tmp_path} ext4 rw 0 0\n")
    r = inuse.in_use(target, ["ws1"], proc_root=str(proc), mounts_file=str(local))
    assert r["verdict"] == "free"


def test_squeue_unavailable_unknown_on_nfs(env, monkeypatch):
    binp, target, proc, mounts = env
    monkeypatch.setenv("PATH", f"{binp}:/nonexistent")
    _exe(binp / "ssh", "exit 255\n")
    r = inuse.in_use(target, [], proc_root=str(proc), mounts_file=str(mounts))
    assert r["verdict"] == "unknown" and "squeue unavailable" in r["evidence"]


def test_remote_match_in_use(env):
    binp, target, proc, mounts = env
    _exe(binp / "squeue", "exit 0\n")
    _exe(binp / "ssh", "cat >/dev/null; echo 'pid 5 python: cwd /x'\n")
    r = inuse.in_use(target, ["ws1"], proc_root=str(proc), mounts_file=str(mounts))
    assert r["verdict"] == "in_use" and r["evidence"][0].startswith("ws1: pid 5")


def test_probe_script_runs(tmp_path):
    import subprocess
    import sys
    p = tmp_path / "t"
    p.mkdir()
    script = f"ROOT = {str(p)!r}\n" + inuse.PROBE_SRC + inuse._PROBE_MAIN
    r = subprocess.run([sys.executable, "-"], input=script, capture_output=True, text=True)
    assert r.returncode == 0
