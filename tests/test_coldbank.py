from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from cdx_agent import coldbank as cb


META = [
    "--meta", "source=test",
    "--meta", "owner=tester",
    "--meta", "regenerable=yes",
    "--meta", "cost=low",
]


def invoke(bank: Path, cache: Path, *args: str, budget: float = 1.0) -> int:
    return cb.main([
        "--bank", str(bank),
        "--cache", str(cache),
        "--budget-gb", str(budget),
        *args,
    ])


def put(bank: Path, cache: Path, source: Path, name: str, *extra: str) -> int:
    return invoke(bank, cache, "put", str(source), name, *META, *extra)


def make_source(root: Path) -> Path:
    source = root / "source"
    (source / "nested").mkdir(parents=True)
    (source / "a.txt").write_text("alpha\n")
    (source / "nested" / "b.txt").write_text("bravo\n")
    return source


def test_list_and_put_metadata_and_refusals(tmp_path, capsys):
    bank = tmp_path / "bank"
    cache = tmp_path / "cache"
    source = make_source(tmp_path)

    assert invoke(bank, cache, "put", str(source), "item", *META) == cb.EXIT_OK
    assert (bank / "item" / "MANIFEST.sha256").is_file()
    assert (bank / "item" / "README.txt").is_file()

    assert invoke(bank, cache, "list") == cb.EXIT_OK
    output = capsys.readouterr().out
    assert "item" in output
    assert "source=test" in output
    assert "regenerable=yes" in output

    assert invoke(bank, cache, "put", str(source), "missing-meta",
                  "--meta", "source=test") == cb.EXIT_REFUSED
    assert not (bank / "missing-meta").exists()

    assert invoke(bank, cache, "put", str(source), "item", *META) == cb.EXIT_REFUSED


def test_fetch_whole_only_and_second_fetch_skips_present_files(tmp_path, monkeypatch, capsys):
    bank = tmp_path / "bank"
    cache = tmp_path / "cache"
    source = make_source(tmp_path)
    assert put(bank, cache, source, "item") == cb.EXIT_OK

    assert invoke(bank, cache, "fetch", "item", "--only", "a.txt", "--no-lease") == cb.EXIT_OK
    first = Path(capsys.readouterr().out.strip().splitlines()[-1])
    assert first == cache / "item"
    assert (cache / "item" / "a.txt").read_text() == "alpha\n"
    assert not (cache / "item" / "nested" / "b.txt").exists()

    def fail_download(*args, **kwargs):
        raise AssertionError("present file should not be downloaded")

    monkeypatch.setattr(cb, "_download_normal", fail_download)
    assert invoke(bank, cache, "fetch", "item", "--only", "a.txt", "--no-lease") == cb.EXIT_OK   # present: no download
    monkeypatch.undo()
    copied = []
    real = cb._download_normal
    monkeypatch.setattr(cb, "_download_normal", lambda info, partial, needed, *a: (copied.extend(needed), real(info, partial, needed, *a)))
    assert invoke(bank, cache, "fetch", "item", "--no-lease") == cb.EXIT_OK
    assert copied == ["nested/b.txt"]                                       # only the missing file is fetched
    assert (cache / "item" / "nested" / "b.txt").read_text() == "bravo\n"


def test_checksum_mismatch_leaves_no_entry_or_partial(tmp_path):
    bank = tmp_path / "bank"
    cache = tmp_path / "cache"
    source = make_source(tmp_path)
    assert put(bank, cache, source, "item") == cb.EXIT_OK

    (bank / "item" / "a.txt").write_text("tampered\n")
    assert invoke(bank, cache, "fetch", "item", "--no-lease") == cb.EXIT_FAIL
    assert not (cache / "item" / ".entry.json").exists()
    partial = cache / ".partial"
    assert not any(partial.iterdir()) if partial.exists() else True


def test_budget_evicts_lru_unleased_entry_and_not_leased_entry(tmp_path):
    source_a = tmp_path / "a"
    source_b = tmp_path / "b"
    source_a.mkdir()
    source_b.mkdir()
    (source_a / "x").write_text("aaaaaa")
    (source_b / "x").write_text("bbbbbb")

    bank = tmp_path / "bank"
    cache = tmp_path / "cache"
    assert put(bank, cache, source_a, "a") == cb.EXIT_OK
    assert put(bank, cache, source_b, "b") == cb.EXIT_OK

    assert invoke(bank, cache, "fetch", "a", "--no-lease", budget=0.00000001) == cb.EXIT_OK
    assert invoke(bank, cache, "fetch", "b", "--no-lease", budget=0.00000001) == cb.EXIT_OK
    assert not (cache / "a" / ".entry.json").exists()
    assert (cache / "b" / ".entry.json").exists()

    cache2 = tmp_path / "cache2"
    assert invoke(bank, cache2, "fetch", "a", budget=0.00000001) == cb.EXIT_OK
    assert invoke(bank, cache2, "fetch", "b", "--no-lease",
                  budget=0.00000001) == cb.EXIT_NO_ROOM
    assert (cache2 / "a" / ".entry.json").exists()


def test_no_room_dry_run_does_not_evict(tmp_path):
    source_a = tmp_path / "a"
    source_b = tmp_path / "b"
    source_a.mkdir()
    source_b.mkdir()
    (source_a / "x").write_text("aaaaaa")
    (source_b / "x").write_text("bbbbbb")

    bank = tmp_path / "bank"
    cache = tmp_path / "cache"
    assert put(bank, cache, source_a, "a") == cb.EXIT_OK
    assert put(bank, cache, source_b, "b") == cb.EXIT_OK

    assert invoke(bank, cache, "fetch", "a", budget=0.00000001) == cb.EXIT_OK
    assert invoke(bank, cache, "fetch", "b", "--dry-run",
                  budget=0.00000001) == cb.EXIT_NO_ROOM
    assert (cache / "a" / ".entry.json").exists()


def test_lease_expiry_and_dead_pid_are_ignored():
    assert not cb.lease_live(
        {"expires": 10, "host": "here", "pid": 123},
        11,
        "here",
        lambda pid: True,
    )
    assert not cb.lease_live(
        {"expires": 20, "host": "here", "pid": 123},
        10,
        "here",
        lambda pid: False,
    )
    assert cb.lease_live(
        {"expires": 20, "host": "elsewhere", "pid": 123},
        10,
        "here",
        lambda pid: False,
    )
    assert cb.lease_live(
        {"expires": 20, "host": "here", "pid": 123, "job": "job-1"},
        10,
        "here",
        lambda pid: False,
    )


def test_run_sets_environment_and_releases_lease(tmp_path, monkeypatch):
    bank = tmp_path / "bank"
    cache = tmp_path / "cache"
    source = make_source(tmp_path)
    assert put(bank, cache, source, "item") == cb.EXIT_OK

    marker = tmp_path / "marker"
    monkeypatch.setenv("COLDBANK_TEST_MARKER", str(marker))
    command = [
        sys.executable,
        "-c",
        (
            "import os, pathlib, sys; "
            "pathlib.Path(os.environ['COLDBANK_TEST_MARKER']).write_text("
            "os.environ['COLDBANK_PATH']); sys.exit(7)"
        ),
    ]
    assert invoke(bank, cache, "run", "item", "--", *command) == 7
    assert marker.read_text() == str(cache / "item")
    leases = cache / ".leases" / "item"
    assert not any(leases.glob("*.json")) if leases.exists() else True


def test_cat_streams_bytes_and_fails_on_mismatch(tmp_path, capsys):
    bank = tmp_path / "bank"
    cache = tmp_path / "cache"
    source = tmp_path / "source"
    source.mkdir()
    (source / "data.txt").write_text("hello\n")
    assert put(bank, cache, source, "item") == cb.EXIT_OK
    capsys.readouterr()

    assert invoke(bank, cache, "cat", "item", "data.txt") == cb.EXIT_OK
    assert capsys.readouterr().out == "hello\n"

    (bank / "item" / "data.txt").write_text("changed\n")
    assert invoke(bank, cache, "cat", "item", "data.txt") == cb.EXIT_FAIL


@pytest.mark.skipif(
    not shutil.which("zstd") or not shutil.which("tar"),
    reason="zstd and tar are required",
)
def test_zstd_put_and_fetch_round_trip(tmp_path, capsys):
    bank = tmp_path / "bank"
    cache = tmp_path / "cache"
    source = make_source(tmp_path)

    assert put(bank, cache, source, "compressed", "--zstd") == cb.EXIT_OK
    assert (bank / "compressed" / "data.tar.zst").is_file()
    assert (bank / "compressed" / "CONTENTS.sha256").is_file()

    assert invoke(bank, cache, "fetch", "compressed", "--no-lease") == cb.EXIT_OK
    path = Path(capsys.readouterr().out.strip().splitlines()[-1])
    assert (path / "a.txt").read_text() == "alpha\n"
    assert (path / "nested" / "b.txt").read_text() == "bravo\n"


def test_bad_names_and_paths_are_refused(tmp_path):
    bank = tmp_path / "bank"
    cache = tmp_path / "cache"
    source = make_source(tmp_path)

    assert invoke(bank, cache, "put", str(source), "../bad", *META) == cb.EXIT_REFUSED
    assert invoke(bank, cache, "fetch", "..") == cb.EXIT_REFUSED
    assert invoke(bank, cache, "cat", "item", "../bad") == cb.EXIT_REFUSED


def test_bank_host_short_circuits_fetch(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cb.socket, "gethostname", lambda: "westeros.example")
    bank = tmp_path / "bank"
    cache = tmp_path / "cache"
    source = make_source(tmp_path)
    spec = "westeros:" + str(bank)

    assert cb.main([
        "--bank", spec,
        "--cache", str(cache),
        "--budget-gb", "1",
        "put", str(source), "item", *META,
    ]) == cb.EXIT_OK

    assert cb.main([
        "--bank", spec,
        "--cache", str(cache),
        "--budget-gb", "1",
        "fetch", "item",
    ]) == cb.EXIT_OK
    assert capsys.readouterr().out.strip().splitlines()[-1] == str(bank / "item")
    assert not cache.exists()


def test_split_streams_balances_bytes():
    streams = cb.split_streams(
        {"a": 100, "b": 90, "c": 80, "d": 70, "e": 60},
        2,
    )
    totals = sorted(sum(size for _, size in stream) for stream in streams)
    assert sum(totals) == 400 and totals[1] - totals[0] <= 100       # greedy (largest first) bound: gap <= largest file


# ---- review fixes (2026-10-28) ---------------------------------------------------------------------------------------
def test_cache_settings_use_this_host_not_the_bank_host(monkeypatch, tmp_path):
    monkeypatch.setattr(cb, "_this_host", lambda: "ilabu")
    monkeypatch.delenv("CDX_COLDCACHE", raising=False)
    args = cb._parser().parse_args(["--bank", "westeros:/data/x", "--config", str(tmp_path / "none.json"), "status"])
    cache, budget = cb._cache_settings(args, "westeros")
    assert cache == "/var/tmp/ss5772/coldcache" and budget == 6 * 1024 ** 3


def test_fetch_takes_a_time_lease_and_release_drops_it(tmp_path):
    bank, cache = tmp_path / "bank", tmp_path / "cache"
    assert put(bank, cache, make_source(tmp_path), "item") == cb.EXIT_OK
    assert invoke(bank, cache, "fetch", "item") == cb.EXIT_OK
    leases = list((cache / ".leases" / "item").glob("*.json"))
    assert len(leases) == 1 and json.loads(leases[0].read_text())["pid"] == 0   # time lease: the caller is often a wrapper
    assert cb._live_leases(str(cache), "item")
    assert invoke(bank, cache, "release", "item") == cb.EXIT_OK   # default: the time leases
    assert not cb._live_leases(str(cache), "item")


def test_slurm_job_lease_is_live_until_expiry(tmp_path, monkeypatch):
    bank, cache = tmp_path / "bank", tmp_path / "cache"
    assert put(bank, cache, make_source(tmp_path), "item") == cb.EXIT_OK
    monkeypatch.setenv("SLURM_JOB_ID", "12345")
    assert invoke(bank, cache, "fetch", "item", "--pid", "999999999") == cb.EXIT_OK   # dead pid, but a job lease
    lease = json.loads(next((cache / ".leases" / "item").glob("*.json")).read_text())
    assert lease["job"] == "12345" and cb._live_leases(str(cache), "item")


def test_put_update_keeps_the_old_version(tmp_path, capsys):
    bank, cache = tmp_path / "bank", tmp_path / "cache"
    source = make_source(tmp_path)
    assert put(bank, cache, source, "item") == cb.EXIT_OK
    (source / "a.txt").write_text("alpha2\n")
    assert put(bank, cache, source, "item", "--update") == cb.EXIT_OK
    olds = list(bank.glob("item.old.*"))
    assert len(olds) == 1 and (olds[0] / "a.txt").read_text() == "alpha\n"
    assert (bank / "item" / "a.txt").read_text() == "alpha2\n"
    assert "old version kept" in capsys.readouterr().out


def test_dry_run_exit_codes(tmp_path):
    bank, cache = tmp_path / "bank", tmp_path / "cache"
    assert put(bank, cache, make_source(tmp_path), "item") == cb.EXIT_OK
    assert invoke(bank, cache, "fetch", "item", "--dry-run") == cb.EXIT_OK                 # fits, entry not there yet
    assert invoke(bank, cache, "fetch", "item", "--dry-run", budget=1e-9) == cb.EXIT_NO_ROOM
    assert not (cache / "item").exists()


def test_free_text_readme_is_listed_not_invalid(tmp_path, capsys):
    bank, cache = tmp_path / "bank", tmp_path / "cache"
    item = bank / "legacy"
    item.mkdir(parents=True)
    (item / "f.bin").write_bytes(b"x" * 10)
    (item / "README.txt").write_text("f.bin = something (hand-written), regenerable\n")
    (item / "MANIFEST.sha256").write_text(f"{cb._hash_file(str(item / 'f.bin'))}  f.bin\n")
    assert invoke(bank, cache, "list") == cb.EXIT_OK
    out = capsys.readouterr().out
    assert "legacy\tfiles=1\tbytes=10" in out and "INVALID" not in out


def test_quota_room_parses_quota_output(monkeypatch):
    out = {"findmnt": "/dev/md2\n",
           "quota": "Disk quotas for user x (uid 1):\n     Filesystem  blocks   quota   limit   grace   files   quota   limit   grace\n"
                    "       /dev/md2 5690656  15728640 10485760            9091       0       0\n"}
    monkeypatch.setattr(cb.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, out[argv[0]], ""))
    assert cb._quota_room("/var/tmp") == (10485760 - 5690656) * 1024        # the smaller nonzero limit counts


def test_remote_bank_item_is_one_ssh_call(monkeypatch, tmp_path):
    calls = []
    payload = ("\n@@COLDBANK@@MANIFEST.sha256\n" + "a" * 64 + "  x/f.bin\n"
               "\n@@COLDBANK@@README.txt\nsource=s\nowner=o\nregenerable=yes\ncost=c"     # no final newline
               "\n@@COLDBANK@@CONTENTS.sha256\n\n@@COLDBANK@@LIST\nx/f.bin\t42\nMANIFEST.sha256\t80\n")
    monkeypatch.setattr(cb, "_ssh_script", lambda host, script: (calls.append(script), payload)[1])
    info = cb._bank_item("westeros", "/bank", False, "item")
    assert len(calls) == 1 and info.files == {"x/f.bin": "a" * 64} and info.sizes["x/f.bin"] == 42
    assert info.meta["cost"] == "c" and not info.compressed
