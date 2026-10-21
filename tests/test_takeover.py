"""--takeover / --drain: resume the cancelled Claude conversation instead of starting fresh, and report
(optionally wait for) the old session's background agents/shells before cancelling."""
import os
import time
from types import SimpleNamespace

from cdx_agent import config as config_mod
from cdx_agent import launch, session


def _cfg(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text("{}")
    cfg = config_mod.Config.defaults(home=home)
    (cfg.tools_root / "hooks").mkdir(parents=True)
    return cfg


def _repo(tmp_path, name="myrepo"):
    repo = tmp_path / name
    repo.mkdir()
    return repo


def _transcript(home, repo, sid, age_s=0.0):
    d = session.claude_transcripts_dir(home, launch.repo_root(repo))
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{sid}.jsonl"
    f.write_text("{}\n")
    t = time.time() - age_s
    os.utime(f, (t, t))
    return f


def _args(repo, passthrough, takeover=True, drain=0.0):
    return SimpleNamespace(
        config=None, repo=str(repo), full=False, safe=True, engine="claude", passthrough=list(passthrough),
        dry_run=True, secondary=False, token_saver=False, cancel_active=False, takeover=takeover, drain=drain,
    )


def _dry_run_tokens(capsys):
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if ln.startswith("DRY_RUN_COMMAND="))
    return line.removeprefix("DRY_RUN_COMMAND=").split(" ")


def test_latest_claude_session_picks_the_newest_transcript(tmp_path):
    cfg, repo = _cfg(tmp_path), _repo(tmp_path)
    _transcript(cfg.account_home, repo, "old-session", age_s=600)
    _transcript(cfg.account_home, repo, "new-session", age_s=5)
    sid, _ = session.latest_claude_session(cfg.account_home, launch.repo_root(repo))
    assert sid == "new-session"


def test_latest_claude_session_none_without_transcripts(tmp_path):
    cfg, repo = _cfg(tmp_path), _repo(tmp_path)
    assert session.latest_claude_session(cfg.account_home, launch.repo_root(repo)) is None


def test_running_background_tasks_uses_output_mtime(tmp_path):
    tdir = tmp_path / "tasks"
    tdir.mkdir()
    old, new = tdir / "a.output", tdir / "b.output"
    old.write_text("x")
    new.write_text("y")
    t_old = time.time() - 1000
    os.utime(old, (t_old, t_old))
    active = session.running_background_tasks([tdir])
    assert active == [new]


def test_claude_task_dirs_found_under_tmp(tmp_path):
    cfg, repo = _cfg(tmp_path), _repo(tmp_path)
    slug = session.claude_repo_slug(launch.repo_root(repo))
    tdir = cfg.account_home / ".tmp" / "claude-123" / slug / "sid-1" / "tasks"
    tdir.mkdir(parents=True)
    assert session.claude_task_dirs(cfg.account_home, launch.repo_root(repo), "sid-1") == [tdir]


def test_drain_returns_immediately_when_quiet_and_times_out_when_busy(tmp_path):
    tdir = tmp_path / "tasks"
    tdir.mkdir()
    f = tdir / "a.output"
    f.write_text("x")
    t_old = time.time() - 1000
    os.utime(f, (t_old, t_old))
    t0 = time.monotonic()
    assert session.drain_background_tasks([tdir], timeout_s=5.0, poll_s=0.05) == []
    assert time.monotonic() - t0 < 1.0
    os.utime(f, None)  # active again
    t0 = time.monotonic()
    assert session.drain_background_tasks([tdir], timeout_s=0.2, poll_s=0.05) == [f]
    assert time.monotonic() - t0 >= 0.2


def test_has_resume_flag():
    assert launch.has_resume_flag(["--resume", "abc"])
    assert launch.has_resume_flag(["-c"])
    assert launch.has_resume_flag(["--resume=abc"])
    assert not launch.has_resume_flag(["-p", "hello"])


def test_takeover_injects_resume_of_latest_session(tmp_path, monkeypatch, capsys):
    cfg, repo = _cfg(tmp_path), _repo(tmp_path)
    _transcript(cfg.account_home, repo, "sess-old", age_s=600)
    _transcript(cfg.account_home, repo, "sess-new", age_s=1)
    monkeypatch.setattr(launch, "load_config", lambda _c: cfg)
    launch.command_launch(_args(repo, []))
    tokens = _dry_run_tokens(capsys)
    i = tokens.index("--resume")
    assert tokens[i + 1] == "sess-new"


def test_takeover_keeps_an_explicit_resume(tmp_path, monkeypatch, capsys):
    cfg, repo = _cfg(tmp_path), _repo(tmp_path)
    _transcript(cfg.account_home, repo, "sess-new", age_s=1)
    monkeypatch.setattr(launch, "load_config", lambda _c: cfg)
    launch.command_launch(_args(repo, ["--resume", "my-choice"]))
    tokens = _dry_run_tokens(capsys)
    assert tokens.count("--resume") == 1
    assert tokens[tokens.index("--resume") + 1] == "my-choice"


def test_takeover_without_transcript_starts_fresh(tmp_path, monkeypatch, capsys):
    cfg, repo = _cfg(tmp_path), _repo(tmp_path)
    monkeypatch.setattr(launch, "load_config", lambda _c: cfg)
    launch.command_launch(_args(repo, []))
    assert "--resume" not in _dry_run_tokens(capsys)


def test_plain_launch_never_injects_resume(tmp_path, monkeypatch, capsys):
    cfg, repo = _cfg(tmp_path), _repo(tmp_path)
    _transcript(cfg.account_home, repo, "sess-new", age_s=1)
    monkeypatch.setattr(launch, "load_config", lambda _c: cfg)
    launch.command_launch(_args(repo, [], takeover=False))
    assert "--resume" not in _dry_run_tokens(capsys)
