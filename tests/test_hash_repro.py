import json
import sys

from cdx_agent import hash_repro

HEX = "ab" * 32


def _cmd(text):
    return [sys.executable, "-c", f"print({text!r})"]


def test_pass_on_prefix_and_json_log(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    log = tmp_path / "log.jsonl"
    rc = hash_repro.main(["--want", "sha256:abab", "--log", str(log), "--", *_cmd(f"identity sha256:{HEX}")])
    out = capsys.readouterr().out
    assert rc == 0 and out.startswith("hash-repro PASS")
    rec = json.loads(log.read_text())
    assert rec["pass"] is True and rec["got"] == [f"sha256:{HEX}"] and rec["exit"] == 0
    assert rec["out"].startswith(str(tmp_path))     # temp file under $TMPDIR


def test_want_without_prefix_and_fail(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    assert hash_repro.main(["--want", "abab", "--", *_cmd(f"sha256:{HEX}")]) == 0
    assert hash_repro.main(["--want", "sha256:ffff", "--", *_cmd(f"sha256:{HEX}")]) == 1
    assert "hash-repro FAIL" in capsys.readouterr().out


def test_no_hash_printed_fails_with_dash(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    assert hash_repro.main(["--want", "sha256:abab", "--", *_cmd("nothing here")]) == 1
    assert "got=-" in capsys.readouterr().out


def test_lone_want_exits_2_with_message(capsys):
    assert hash_repro.main(["--want"]) == 2
    assert "--want needs a value" in capsys.readouterr().err
    assert hash_repro.main(["--want", "--", "true"]) == 2


def test_usage_errors_exit_2(capsys):
    assert hash_repro.main([]) == 2
    assert hash_repro.main(["--want", "sha256:ab"]) == 2                  # no command
    assert hash_repro.main(["--bogus"]) == 2
    assert hash_repro.main(["--want", "x", "--timeout", "abc", "--", "true"]) == 2
    assert "unknown argument --bogus" in capsys.readouterr().err


def test_timeout_and_missing_command(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    assert hash_repro.main(["--want", "sha256:abab", "--timeout", "0.5", "--", sys.executable, "-c", "import time; time.sleep(20)"]) == 1
    assert "exit=124" in capsys.readouterr().out
    assert hash_repro.main(["--want", "sha256:abab", "--", "definitely-not-a-command-xyz"]) == 1
    assert "exit=127" in capsys.readouterr().out


def test_child_runs_with_cuda_hidden_and_two_threads(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")
    code = "import os; print('sha256:' + ('%02x' % len(os.environ['CUDA_VISIBLE_DEVICES'])) * 4 + os.environ['OMP_NUM_THREADS'] * 4)"
    assert hash_repro.main(["--want", "sha256:0000", "--", sys.executable, "-c", code]) == 0
