"""cdx_agent.gpu_lock: lock protocol, owner files, NFS snapshot, node-run lifecycle, status, srun command.

Runs locally against temp lock dirs only; no srun / ssh / scancel. pytest's tmp dir may be NFS, where flock is
emulated with per-process POSIX locks, so "someone else holds it" is always simulated from another process.
"""
from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cdx_agent import gpu_lock as gl

WRAPPER = Path(gl.__file__).resolve()


class Holder:
    """Another process holding flocks on the given files until closed."""

    def __init__(self, *paths: Path) -> None:
        code = ("import fcntl,os,sys\nfds=[]\nfor p in sys.argv[1:]:\n"
                "    fd=os.open(p,os.O_WRONLY|os.O_CREAT,0o640); fcntl.flock(fd,fcntl.LOCK_EX); fds.append(fd)\n"
                "print('ready',flush=True); sys.stdin.read()")
        self.proc = subprocess.Popen([sys.executable, "-c", code, *map(str, paths)], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, text=True)
        assert self.proc.stdout.readline().strip() == "ready"

    def close(self) -> None:
        self.proc.stdin.close()
        self.proc.wait(timeout=10)


def _locked_elsewhere(path: Path) -> bool:
    """True if a fresh process cannot take the lock (i.e. someone holds it)."""
    code = "import fcntl,os,sys\nfd=os.open(sys.argv[1],os.O_WRONLY|os.O_CREAT)\ntry:\n" \
           "    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)\nexcept OSError:\n    sys.exit(1)\n"
    return subprocess.run([sys.executable, "-c", code, str(path)]).returncode == 1


def test_acquire_lowest_free_first_and_all_or_nothing(tmp_path):
    d = tmp_path / "locks"
    h = Holder(_mk(d, 0))  # another session holds gpu0
    try:
        taken = gl.acquire(str(d), [0, 1, 2, 3], 2)
        assert [g for g, _ in taken] == [1, 2]
        assert _locked_elsewhere(Path(gl.lock_path(str(d), 1)))
        with pytest.raises(gl.LockError) as ei:  # a third session wants 2: only gpu3 is left -> all-or-nothing
            _acquire_in_child(d, "0-3", 2)
        assert ei.value.code == gl.EXIT_NO_LOCK
        assert not _locked_elsewhere(Path(gl.lock_path(str(d), 3)))  # its partial grab was not kept
        gl.release(taken)
        assert not _locked_elsewhere(Path(gl.lock_path(str(d), 1)))
    finally:
        h.close()


def _mk(d: Path, g: int) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    return Path(gl.lock_path(str(d), g))


def _acquire_in_child(d: Path, gpus: str, n: int) -> None:
    """acquire() from a separate process (so in-process locks above count as someone else's)."""
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import gpu_lock as gl\n"
            "try:\n    gl.acquire(sys.argv[2], gl.parse_gpus(sys.argv[3]), int(sys.argv[4]))\n"
            "except gl.LockError as e:\n    sys.exit(e.code)\n")
    rc = subprocess.run([sys.executable, "-c", code, str(WRAPPER.parent), str(d), gpus, str(n)]).returncode
    if rc:
        raise gl.LockError(rc, "child acquire failed")


def test_busy_gpu_is_skipped_and_reported(tmp_path):
    d = str(tmp_path / "locks")
    taken = gl.acquire(d, [0, 1], 1, busy_mib={0: 9000, 1: 3})
    assert [g for g, _ in taken] == [1]
    gl.release(taken)
    with pytest.raises(gl.LockError) as ei:
        gl.acquire(d, [0, 1], 1, busy_mib={0: 9000, 1: 9000})
    assert ei.value.code == gl.EXIT_BUSY and "gpu0=9000MiB" in str(ei.value)


def test_owner_file_written_and_removed_only_by_owner(tmp_path):
    d = str(tmp_path)
    line = gl.owner_line("jenga", 2, 4242, "rlab2", "526576.7", "2026-10-16T10:00:00-04:00", "unknown",
                         ["bash", "run me.sh"])
    gl.write_owner(d, 2, line)
    text = Path(gl.owner_path(d, 2)).read_text()
    assert text.startswith("jenga host=rlab2 pid=4242 step=526576.7 gpu=2 since=") and "cmd=bash 'run me.sh'" in text
    assert not gl.remove_owner(d, 2, 999)  # someone else's
    assert Path(gl.owner_path(d, 2)).exists()
    assert gl.remove_owner(d, 2, 4242)
    assert not Path(gl.owner_path(d, 2)).exists()


# ----------------------------------------------------------------------------------------- NFS snapshot
def _script(path: Path, body: str, shebang: str = "#!/usr/bin/env bash") -> Path:
    path.write_text(f"{shebang}\n{body}\n")
    path.chmod(0o750)
    return path


def test_snapshot_copies_shell_scripts_and_isolates_from_later_edits(tmp_path):
    run = tmp_path / "run"
    s = _script(tmp_path / "job.sh", "echo v1")
    for cmd, idx in ([["bash", str(s), "a"], 1], [["bash", "-x", "-o", "pipefail", str(s)], 4],
                     [["sh", "--", str(s)], 2], [[str(s), "a"], 0]):
        new, copies, env = gl.snapshot_command(cmd, str(run))
        assert len(copies) == 1 and new[idx] == copies[0] and copies[0].startswith(str(run))
        assert [c for i, c in enumerate(new) if i != idx] == [c for i, c in enumerate(cmd) if i != idx]
        assert env == {"CDX_GPU_LOCK_SCRIPT_ORIG": str(s), "CDX_GPU_LOCK_SCRIPT_DIR": str(tmp_path)}
        assert os.access(copies[0], os.X_OK)
    tmp = tmp_path / "job.sh.new"  # the edit-by-mv that killed step 526576.16
    _script(tmp, "echo v2")
    os.replace(tmp, s)
    assert "echo v1" in Path(copies[0]).read_text()


def test_snapshot_leaves_non_shell_commands_alone(tmp_path):
    py = _script(tmp_path / "train.py", "print(1)", shebang="#!/usr/bin/env python3")
    for cmd in (["bash", "-lc", "echo hi"], ["bash", "-c", "x"], ["python3", str(py)], [str(py)], ["nvidia-smi"],
                ["bash", str(tmp_path / "missing.sh")], []):
        new, copies, env = gl.snapshot_command(cmd, str(tmp_path / "run"))
        assert new == list(cmd) and copies == [] and env == {}
    assert not (tmp_path / "run").exists()


# ----------------------------------------------------------------------------------------- node-run lifecycle
def _node_run(lock_dir: Path, *args: str, cmd, **kw) -> subprocess.Popen:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SLURM_", "CDX_GPU_LOCK"))}
    return subprocess.Popen([sys.executable, str(WRAPPER), "node-run", "--lock-dir", str(lock_dir), "--force-busy",
                             *args, "--", *cmd], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, **kw)


def test_node_run_locks_snapshots_runs_and_cleans_up(tmp_path):
    locks = tmp_path / "locks"
    h = Holder(_mk(locks, 0))  # another session has gpu0
    script = _script(tmp_path / "step.sh", "\n".join([
        'echo "cvd=$CUDA_VISIBLE_DEVICES"', 'echo "self=$0"', 'echo "wrapper=$CDX_GPU_LOCK_WRAPPER"',
        'echo "orig=$CDX_GPU_LOCK_SCRIPT_ORIG"', f'echo "owner=$(cat {locks}/gpu1.owner)"',
        f'flock -n {locks}/gpu1.lock true && echo held=no || echo held=yes', "exit 7"]))
    try:
        p = _node_run(locks, "--gpus", "0-2", "--n", "1", "--who", "testproj", "--eta", "5", cmd=["bash", str(script)])
        out, err = p.communicate(timeout=60)
    finally:
        h.close()
    assert p.returncode == 7, err
    kv = dict(line.split("=", 1) for line in out.splitlines())
    run_root = str(locks / "run") + "/"
    assert kv["cvd"] == "1"
    assert kv["self"].startswith(run_root) and kv["self"].endswith("/step.sh")       # ran the node-local copy
    assert kv["wrapper"].startswith(run_root) and kv["wrapper"].endswith("/gpu_lock.py")  # wrapper re-exec'd
    assert kv["orig"] == str(script)
    assert kv["owner"].startswith(f"testproj host=") and f" pid={p.pid} " in kv["owner"] and "eta=20" in kv["owner"]
    assert kv["held"] == "yes"
    assert not (locks / "gpu1.owner").exists()                      # owner removed
    assert not _locked_elsewhere(locks / "gpu1.lock")               # lock released
    assert list((locks / "run").iterdir()) == []                    # snapshot dir cleaned
    assert "locked gpu 1" in err


def test_node_run_no_free_lock_exits_3(tmp_path):
    locks = tmp_path / "locks"
    h = Holder(_mk(locks, 0), _mk(locks, 1))
    try:
        p = _node_run(locks, "--gpus", "0,1", cmd=["true"])
        out, err = p.communicate(timeout=60)
    finally:
        h.close()
    assert p.returncode == gl.EXIT_NO_LOCK and "need 1 free GPU lock" in err
    assert list((locks / "run").iterdir()) == []


def test_node_run_sigterm_releases(tmp_path):
    locks = tmp_path / "locks"
    p = _node_run(locks, "--gpus", "0", "--no-snapshot", cmd=["sleep", "30"])
    deadline = time.time() + 30
    while not (locks / "gpu0.owner").exists() and time.time() < deadline:
        time.sleep(0.05)
    assert (locks / "gpu0.owner").exists()
    p.send_signal(signal.SIGTERM)
    p.communicate(timeout=30)
    assert p.returncode == 128 + signal.SIGTERM
    assert not (locks / "gpu0.owner").exists() and not _locked_elsewhere(locks / "gpu0.lock")


# ----------------------------------------------------------------------------------------- status
def test_status_from_proc_locks(tmp_path):
    d = tmp_path / "locks"
    for g in (0, 1, 2):
        _mk(d, g).touch()
    gl.write_owner(str(d), 1, "bimanual host=rlab2 pid=1 step=1.2 gpu=1 since=x eta=y cmd=z\n")
    gl.write_owner(str(d), 2, "telecom host=rlab2 pid=2 step=1.3 gpu=2 since=x eta=y cmd=z\n")
    st = os.stat(gl.lock_path(str(d), 1))
    fake = (f"1: FLOCK  ADVISORY  WRITE 4242 {os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino} 0 EOF\n"
            f"1: -> FLOCK  ADVISORY  WRITE 99 00:00:{st.st_ino} 0 EOF\n")
    lines = gl.status_lines(str(d), proc_locks=fake)
    assert lines[1] == "  gpu0 free"
    assert lines[2].startswith("  gpu1 LOCKED (flock pid 4242) owner: bimanual")
    assert lines[3].startswith("  gpu2 free (stale owner file: telecom")
    assert gl.status_lines(str(tmp_path / "none"))[0].startswith("no lock dir")


def test_status_cli_local(tmp_path, capsys):
    d = tmp_path / "locks"
    _mk(d, 0).touch()
    assert gl.main(["--status", "--node", "local", "--lock-dir", str(d)]) == 0
    assert "gpu0 free" in capsys.readouterr().out


# ----------------------------------------------------------------------------------------- submit side
def test_parse_gres_variants():
    assert gl.parse_gres("gres/gpu:a100:4") == "gpu:a100:4"
    assert gl.parse_gres("gres:gpu:4") == "gpu:4"
    assert gl.parse_gres("gres/gpu:a100=2") == "gpu:a100:2"
    assert gl.parse_gres("N/A") is None


def test_dry_run_prints_srun_inside_hold_and_never_execs(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(os, "execvp", lambda *a: pytest.fail("dry-run must not exec"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("no ssh/squeue when --gres is given"))
    rc = gl.main(["526576", "--n", "2", "--eta", "45", "--gres", "gpu:a100:4", "--who", "jenga", "--dry-run",
                  "--", "bash", "/x/run job.sh", "--flag"])
    assert rc == 0
    line = capsys.readouterr().out.splitlines()[0]
    argv = shlex.split(line)
    assert argv[:4] == ["ssh", "-o", "BatchMode=yes", "rlab1"]
    remote = shlex.split(argv[4])
    assert remote[:7] == ["srun", "--jobid=526576", "--overlap", "--gres=gpu:a100:4", "--mem=16G", "-t", "90"]
    assert remote[7:10] == ["python3", str(WRAPPER), "node-run"]
    i = remote.index("--")
    assert remote[i + 1:] == ["bash", "/x/run job.sh", "--flag"]
    opts = remote[10:i]
    assert opts[opts.index("--n") + 1] == "2" and opts[opts.index("--gpus") + 1] == "0-3"
    assert opts[opts.index("--lock-dir") + 1] == gl.DEFAULT_LOCK_DIR


def test_submit_usage_errors(capsys):
    assert gl.main(["526576", "--gres", "gpu:a100:4", "--dry-run"]) == gl.EXIT_USAGE      # no CMD
    assert gl.main(["abc", "--gres", "gpu:4", "--dry-run", "--", "true"]) == gl.EXIT_USAGE  # bad job id
    assert gl.main(["1", "--gres", "gpu:2", "--n", "3", "--dry-run", "--", "true"]) == gl.EXIT_USAGE
