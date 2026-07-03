"""First direct tests for token_tools.py -- previously exercised only
indirectly through the CLI. Pins the bug-hunt fixes: the --tail-lines 0
whole-file dump, safe-rg's no-matches exit code, and the missing-input crash.
"""
from __future__ import annotations

import shutil
from types import SimpleNamespace
from pathlib import Path

import pytest

from cdx_agent import token_tools


def _summarize_log_args(path: Path, head: int = 5, tail: int = 5) -> SimpleNamespace:
    return SimpleNamespace(input=str(path), head_lines=head, tail_lines=tail)


def _summarize_output_args(path: Path, head: int = 5, tail: int = 5) -> SimpleNamespace:
    return SimpleNamespace(input=str(path), command="", exit_code=0, head_lines=head, tail_lines=tail)


def test_tail_helper_zero_means_no_lines():
    # lines[-0:] is the whole list -- the exact token blowup these
    # summarizers exist to prevent.
    lines = [f"line {i}" for i in range(100)]
    assert token_tools._tail(lines, 0) == []
    assert token_tools._tail(lines, 3) == ["line 97", "line 98", "line 99"]
    assert token_tools._tail(lines, -1) == []


def test_summarize_log_tail_zero_does_not_dump_file(tmp_path, capsys):
    log = tmp_path / "big.log"
    log.write_text("\n".join(f"noise {i}" for i in range(500)))
    rc = token_tools.command_summarize_log(_summarize_log_args(log, head=2, tail=0))
    out = capsys.readouterr().out
    assert rc == 0
    assert "noise 499" not in out  # the old bug echoed the entire file
    assert "noise 0" in out  # head still shown


def test_summarize_output_tail_zero_does_not_dump_file(tmp_path, capsys):
    log = tmp_path / "out.txt"
    log.write_text("\n".join(f"unique-token-{i}" for i in range(500)))
    rc = token_tools.command_summarize_output(_summarize_output_args(log, head=2, tail=0))
    out = capsys.readouterr().out
    assert rc == 0
    assert "unique-token-499" not in out


def test_summarize_output_missing_input_degrades(tmp_path, capsys):
    rc = token_tools.command_summarize_output(_summarize_output_args(tmp_path / "nope.txt"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "Bytes: 0" in out
    assert "Line count: 0" in out


def test_summarize_log_missing_input_degrades(tmp_path, capsys):
    rc = token_tools.command_summarize_log(_summarize_log_args(tmp_path / "nope.log"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "Bytes: 0" in out


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_safe_rg_no_matches_is_success(tmp_path, capsys):
    (tmp_path / "a.txt").write_text("hello\n")
    args = SimpleNamespace(pattern="definitely_not_present_xyz", paths=[str(tmp_path)], max_lines=50)
    rc = token_tools.command_safe_rg(args)
    out = capsys.readouterr().out
    assert rc == 0  # rg exits 1 for no-matches; that's not a tool failure
    assert "0 matches" in out


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_safe_rg_matches_found(tmp_path, capsys):
    (tmp_path / "a.txt").write_text("needle in here\n")
    args = SimpleNamespace(pattern="needle", paths=[str(tmp_path)], max_lines=50)
    rc = token_tools.command_safe_rg(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "needle" in out


def test_compress_runs_collapses_repeats():
    lines = ["setup"] + ["downloading 5%"] * 10 + ["done"]
    out = token_tools._compress_runs(lines)
    assert "[repeated 10x] downloading 5%" in out
    assert out.count("downloading 5%") == 0 or "[repeated" in "".join(out)
