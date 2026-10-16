"""cdx_agent.move_verify: copy, prove the copy, never delete; print the user's rm+ln line."""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cdx_agent import move_verify as mv

pytestmark = pytest.mark.skipif(not __import__("shutil").which("rsync"), reason="rsync not installed")


def _tree(root: Path) -> Path:
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "f1.txt").write_text("hello\n")
    (root / "a" / "b" / "f2.bin").write_bytes(os.urandom(4096))
    (root / "empty_dir").mkdir()
    os.symlink("a/f1.txt", root / "link")
    return root


def test_happy_path_prints_verified_and_user_line(tmp_path, capsys):
    src = _tree(tmp_path / "src")
    dst = tmp_path / "new" / "dst"
    (tmp_path / "new").mkdir()
    rc = mv.main([str(src), str(dst)])
    out = capsys.readouterr().out
    assert rc == mv.EXIT_OK, out
    assert "VERIFIED" in out and "2 files, 4102 bytes" in out and "1 symlinks" in out
    expected = f"! rm -rf {shlex.quote(str(src))} && ln -s {shlex.quote(str(dst))} {shlex.quote(str(src))}"
    assert out.rstrip().splitlines()[-1] == expected
    assert (src / "a" / "f1.txt").exists()  # nothing deleted
    assert (dst / "a" / "b" / "f2.bin").read_bytes() == (src / "a" / "b" / "f2.bin").read_bytes()
    assert os.readlink(dst / "link") == "a/f1.txt"


def test_refusals(tmp_path, capsys):
    src = _tree(tmp_path / "src")
    os.symlink(src, tmp_path / "srclink")
    assert mv.main([str(tmp_path / "srclink"), str(tmp_path / "d")]) == mv.EXIT_REFUSED
    assert mv.main([str(src), str(src / "inner")]) == mv.EXIT_REFUSED
    assert mv.main([str(src / "a"), str(src)]) == mv.EXIT_REFUSED
    assert mv.main([str(tmp_path / "missing"), str(tmp_path / "d")]) == mv.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "already a symlink" in err and "inside SRC" in err and "inside DST" in err
    assert not (tmp_path / "d").exists() and not (src / "inner").exists()
    assert mv.check_paths("/", str(tmp_path / "d")) is not None
    assert mv.check_paths("/common/users", str(tmp_path / "d")) is not None


def test_refuses_while_a_file_under_src_is_open(tmp_path, capsys):
    src = _tree(tmp_path / "src")
    holder = subprocess.Popen([sys.executable, "-c", f"f=open({str(src / 'a' / 'f1.txt')!r}); "
                               "import sys; print('ready', flush=True); sys.stdin.read()"],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "ready"
        assert mv.main([str(src), str(tmp_path / "dst")]) == mv.EXIT_REFUSED
        assert f"pid {holder.pid}" in capsys.readouterr().err
        assert not (tmp_path / "dst").exists()
        assert mv.main([str(src), str(tmp_path / "dst"), "--no-lsof"]) == mv.EXIT_OK
    finally:
        holder.stdin.close()
        holder.wait(timeout=10)


def test_verify_catches_extra_files_and_same_size_content_change(tmp_path, capsys):
    src = _tree(tmp_path / "src")
    dst = tmp_path / "dst"
    assert mv.main([str(src), str(dst), "--no-lsof"]) == mv.EXIT_OK
    (dst / "extra.txt").write_text("x")
    assert mv.main([str(src), str(dst), "--no-lsof", "--verify-only"]) == mv.EXIT_FAIL
    assert "files differ: SRC 2 vs DST 3" in capsys.readouterr().out
    (dst / "extra.txt").unlink()
    f = dst / "a" / "f1.txt"
    st = f.stat()
    f.write_text("HELLO\n")  # same size, then restore mtime: only --checksum can see it
    os.utime(f, (st.st_atime, st.st_mtime))
    assert mv.main([str(src), str(dst), "--no-lsof", "--verify-only"]) == mv.EXIT_FAIL
    out = capsys.readouterr().out
    assert "checksum dry-run lists 1 difference" in out and "a/f1.txt" in out


def test_tree_stats_counts_file_bytes_only(tmp_path):
    root = _tree(tmp_path / "t")
    s = mv.tree_stats(str(root))
    assert (s.files, s.bytes, s.symlinks, s.dirs, s.other) == (2, 6 + 4096, 1, 3, 0)


def test_copy_command_uses_idle_priority_and_bwlimit():
    cmd = mv.copy_command("/x/src/", "/y/dst", 5000)
    assert cmd[-4:] == ["-a", "--bwlimit=5000", "/x/src/", "/y/dst/"]
    assert "rsync" in cmd and "--delete" not in " ".join(cmd)
