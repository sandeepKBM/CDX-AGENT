import os
import signal
import subprocess
import sys
import time

import pytest

from cdx_agent import session


def _spawn_lock_holder(lock_path, hold_seconds=30):
    code = (
        "import time\n"
        "from cdx_agent import session\n"
        f"h = session.acquire_lock({str(lock_path)!r})\n"
        "if h is None:\n"
        "    raise SystemExit(3)\n"
        f"time.sleep({hold_seconds})\n"
    )
    return subprocess.Popen([sys.executable, "-c", code])


def _wait_until(predicate, timeout=5.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# --- lock acquisition ----------------------------------------------------------------


def test_acquire_lock_writes_pid(tmp_path):
    lock_path = tmp_path / "lock"
    handle = session.acquire_lock(lock_path)
    try:
        assert handle is not None
        assert handle.pid == os.getpid()
        assert session.read_lock_pid(lock_path) == os.getpid()
    finally:
        session.release_lock(handle)


def test_acquire_lock_fails_when_already_held_by_another_process(tmp_path):
    lock_path = tmp_path / "lock"
    holder = _spawn_lock_holder(lock_path, hold_seconds=5)
    try:
        assert _wait_until(lambda: session.read_lock_pid(lock_path) == holder.pid)
        assert session.acquire_lock(lock_path) is None
    finally:
        holder.terminate()
        holder.wait(timeout=5)


def test_release_lock_allows_reacquisition(tmp_path):
    lock_path = tmp_path / "lock"
    handle = session.acquire_lock(lock_path)
    session.release_lock(handle)
    handle2 = session.acquire_lock(lock_path)
    assert handle2 is not None
    session.release_lock(handle2)


# --- diagnosis -----------------------------------------------------------------------


def test_diagnose_session_no_lock_file(tmp_path):
    repo = tmp_path / "repo"
    runtime_dir = tmp_path / "runtime"
    diagnosis = session.diagnose_session(repo, runtime_dir)
    assert diagnosis.lock_pid is None
    assert diagnosis.owner_alive is False
    assert diagnosis.owner_verified is False
    assert diagnosis.runtime_exists is False


def test_diagnose_session_verifies_live_holder(tmp_path):
    repo = tmp_path / "repo"
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    lock_path = session.lock_path_for(runtime_dir)
    holder = _spawn_lock_holder(lock_path, hold_seconds=5)
    try:
        assert _wait_until(lambda: session.read_lock_pid(lock_path) == holder.pid)
        assert _wait_until(lambda: session.is_lock_held_by(lock_path, holder.pid))
        diagnosis = session.diagnose_session(repo, runtime_dir)
        assert diagnosis.lock_pid == holder.pid
        assert diagnosis.owner_alive is True
        assert diagnosis.owner_verified is True
    finally:
        holder.terminate()
        holder.wait(timeout=5)


# --- the A3 fix: PID-verified cancellation --------------------------------------------


def test_cancel_does_not_kill_unrelated_process_with_matching_cwd_string(tmp_path):
    # This is the regression test for the actual incident: the bash version
    # matched processes by substring on the repo path anywhere in `ps` output.
    # Here, a process whose argv contains the repo path but which never took
    # the session lock must never be signaled.
    repo = tmp_path / "myrepo"
    repo.mkdir()
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()  # stale/orphaned dir, no lock ever taken

    decoy = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", "--", str(repo)]
    )
    try:
        result = session.cancel_session(repo, runtime_dir)
        assert result.action == "cleaned_up_only"
        assert result.signaled_pids == ()
        time.sleep(0.3)
        assert decoy.poll() is None
    finally:
        decoy.terminate()
        decoy.wait(timeout=5)


def test_cancel_kills_actual_lock_holder(tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    lock_path = session.lock_path_for(runtime_dir)

    holder = _spawn_lock_holder(lock_path, hold_seconds=30)
    try:
        assert _wait_until(
            lambda: session.read_lock_pid(lock_path) == holder.pid
            and session.is_lock_held_by(lock_path, holder.pid)
        )

        result = session.cancel_session(repo, runtime_dir)
        assert result.action == "killed"
        assert holder.pid in result.signaled_pids
        holder.wait(timeout=5)
        assert holder.returncode is not None
        assert result.moved_to is not None
        assert not runtime_dir.exists()
        assert result.moved_to.is_dir()
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=5)


def test_cancel_refuses_to_signal_pid_that_no_longer_holds_the_lock(tmp_path):
    # Simulates a stale/foreign lock file: it names a real, live process, but
    # that process never actually holds this flock. The old bash heuristic
    # would have matched and killed anything with the repo path in its argv;
    # this must refuse instead of guessing.
    repo = tmp_path / "myrepo"
    repo.mkdir()
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    lock_path = session.lock_path_for(runtime_dir)

    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        lock_path.write_text(f"{bystander.pid}\n")
        result = session.cancel_session(repo, runtime_dir)
        assert result.action == "refused"
        time.sleep(0.3)
        assert bystander.poll() is None
    finally:
        bystander.terminate()
        bystander.wait(timeout=5)


def test_cancel_cleans_up_only_when_lock_pid_already_dead(tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    lock_path = session.lock_path_for(runtime_dir)

    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=5)
    lock_path.write_text(f"{dead.pid}\n")

    result = session.cancel_session(repo, runtime_dir)
    assert result.action == "cleaned_up_only"
    assert result.signaled_pids == ()
    assert not runtime_dir.exists()
    assert result.moved_to.is_dir()


def test_cancel_dry_run_never_signals_or_moves(tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    lock_path = session.lock_path_for(runtime_dir)

    holder = _spawn_lock_holder(lock_path, hold_seconds=10)
    try:
        assert _wait_until(
            lambda: session.read_lock_pid(lock_path) == holder.pid
            and session.is_lock_held_by(lock_path, holder.pid)
        )
        result = session.cancel_session(repo, runtime_dir, dry_run=True)
        assert result.action == "killed"
        assert holder.pid in result.signaled_pids
        assert holder.poll() is None  # dry-run: still alive
        assert runtime_dir.is_dir()  # dry-run: not moved
    finally:
        holder.terminate()
        holder.wait(timeout=5)


def test_handle_conflict_defaults_to_diagnose_only(tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    lock_path = session.lock_path_for(runtime_dir)

    holder = _spawn_lock_holder(lock_path, hold_seconds=10)
    try:
        assert _wait_until(lambda: session.read_lock_pid(lock_path) == holder.pid)
        diagnosis, cancel_result = session.handle_conflict(repo, runtime_dir)
        assert cancel_result is None
        assert diagnosis.lock_pid == holder.pid
        assert holder.poll() is None  # never signaled -- diagnose only
    finally:
        holder.terminate()
        holder.wait(timeout=5)


def test_handle_conflict_cancel_mode_signals(tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    lock_path = session.lock_path_for(runtime_dir)

    holder = _spawn_lock_holder(lock_path, hold_seconds=30)
    try:
        assert _wait_until(
            lambda: session.read_lock_pid(lock_path) == holder.pid
            and session.is_lock_held_by(lock_path, holder.pid)
        )
        diagnosis, cancel_result = session.handle_conflict(repo, runtime_dir, mode="cancel")
        assert cancel_result.action == "killed"
        holder.wait(timeout=5)
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=5)


# --- child process discovery -----------------------------------------------------------


def test_child_pids_finds_direct_children():
    parent_script = (
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "print(p.pid, flush=True)\n"
        "time.sleep(30)\n"
    )
    parent = subprocess.Popen([sys.executable, "-c", parent_script], stdout=subprocess.PIPE, text=True)
    child_pid = None
    try:
        line = parent.stdout.readline().strip()
        child_pid = int(line)
        assert _wait_until(lambda: child_pid in session.child_pids(parent.pid))
    finally:
        parent.kill()
        parent.wait(timeout=5)
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except OSError:
                pass


def test_pid_is_alive(tmp_path):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        assert session.pid_is_alive(proc.pid) is True
    finally:
        proc.kill()
        proc.wait(timeout=5)
    assert session.pid_is_alive(proc.pid) is False


# --- session log discovery --------------------------------------------------------------


def test_session_root_candidates_dedupes(tmp_path):
    account_home = tmp_path / "home"
    runtime_dir = tmp_path / "runtime"
    roots = session.session_root_candidates(account_home, runtime_dir, legacy_runtime_dir=runtime_dir)
    assert roots == [account_home / ".codex", runtime_dir]


def test_session_jsonl_files_and_recent_count(tmp_path):
    root = tmp_path / "sessions"
    nested = root / "2026" / "06" / "30"
    nested.mkdir(parents=True)
    recent = nested / "rollout-1.jsonl"
    recent.write_text("{}\n")
    old = nested / "rollout-old.jsonl"
    old.write_text("{}\n")
    old_time = time.time() - 90 * 86400
    os.utime(old, (old_time, old_time))

    files = session.session_jsonl_files([root])
    assert recent in files
    assert old in files
    assert session.recent_session_jsonl_count([root], within_days=30) == 1


def test_launch_log_dir_uses_sanitized_repo_name(tmp_path):
    logdir = session.launch_log_dir(tmp_path, "my repo!!")
    assert logdir.parent == tmp_path / "codex_logs"
    assert "my_repo" in logdir.name


@pytest.mark.skipif(session.fcntl is None, reason="requires POSIX fcntl")
def test_fcntl_is_available_on_this_platform():
    assert session.fcntl is not None
