"""Launch orchestration for both engines (codex, claude).

Implements **D1**: `launch()` is engine-parameterized and shares runtime
provisioning (`runtime.py`), session locking (`session.py`), skill linking
(`skills.py`), and hooks/doc generation (`hooks.py`/`context_docs.py`)
identically between `codex` and `claude` -- previously only Codex could be
launched through this tool at all.

Codex and Claude Code do not have flag-for-flag equivalent sandbox/permission
models, so `access_mode` is mapped per engine rather than assumed identical:

- **codex**: `full` -> ``--sandbox danger-full-access``, `safe` -> ``--sandbox
  workspace-write``; approval is always ``on-request`` (matches the bash
  predecessor).
- **claude**: `full` -> ``--permission-mode bypassPermissions`` (closest
  analog to unrestricted filesystem access), `safe` -> ``--permission-mode
  default`` (normal interactive permission prompts, closest analog to
  workspace-write + on-request approval). This mapping is a documented
  judgment call, not a spec guarantee -- revisit if Claude Code's permission
  model changes.

Claude Code has no `-C <path>`-style flag the way Codex does; it operates on
the process's current working directory, so `launch()` sets `cwd=repo`
explicitly for both engines rather than relying on a CLI flag.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import context_docs, hooks, hygiene, project, session, skills
from .config import Config, load_config, repo_root
from .runtime import AccessMode, Engine, RuntimeContext, provision_runtime

CODEX_APPROVAL = "on-request"

# The single source of truth for which engine `cdx-agent` launches when no
# --engine flag is given. Change this to switch the default -- every CLI
# parser default in cli.py reads from here rather than hardcoding a literal,
# so there's exactly one place to edit.
DEFAULT_ENGINE: Engine = "claude"

SANDBOX_BY_ACCESS: dict[AccessMode, str] = {
    "full": "danger-full-access",
    "safe": "workspace-write",
}

PERMISSION_MODE_BY_ACCESS: dict[AccessMode, str] = {
    "full": "bypassPermissions",
    "safe": "default",
}


def translate_passthrough_for_engine(engine: Engine, passthrough: list[str]) -> list[str]:
    """Codex has a `resume` SUBCOMMAND (`codex resume [--last] [SESSION_ID]
    [PROMPT]`); Claude Code has no such subcommand -- resuming is a `-c`/
    `--continue` or `-r`/`--resume [sessionId]` FLAG instead. Someone typing
    `cdx-agent resume` out of Codex muscle memory (very plausible now that
    Claude is the default engine) would otherwise silently get "resume"
    forwarded as a literal Claude prompt -- confirmed live: `claude resume`
    starts a fresh session that just replies conversationally to the word
    "resume", it does not resume anything. Only fires for the exact
    Codex-subcommand shape (`resume` as the first token); anything else
    (e.g. already using `-c`/`--resume`) passes through untouched."""
    if engine != "claude" or not passthrough or passthrough[0] != "resume":
        return passthrough
    rest = passthrough[1:]
    if "--last" in rest:
        return ["--continue", *(tok for tok in rest if tok != "--last")]
    # A bare session id/name after `resume` rides along as --resume's value;
    # to send the literal word "resume" as a prompt instead, use `-p resume`.
    return ["--resume", *rest]


def build_codex_command(repo: Path, access_mode: AccessMode, passthrough: list[str]) -> list[str]:
    return [
        "codex",
        "-C",
        str(repo),
        "--sandbox",
        SANDBOX_BY_ACCESS[access_mode],
        "--ask-for-approval",
        CODEX_APPROVAL,
        "-c",
        'approvals_reviewer="auto_review"',
        *passthrough,
    ]


def build_claude_command(
    repo: Path, access_mode: AccessMode, passthrough: list[str], runtime_dir: Path | None = None
) -> list[str]:
    """Claude Code has no CODEX_HOME-style single env var that redirects its
    whole config/skills home to an arbitrary directory, so getting it to
    actually use what cdx-agent provisions takes three separate,
    empirically-verified mechanisms instead of one:

    - ``--add-dir <runtime_dir>``: extends skill discovery to
      ``<runtime_dir>/.claude/skills/*/SKILL.md`` (confirmed by a live test:
      a marker skill placed there showed up in Claude's available-skills
      list). Does NOT auto-load a CLAUDE.md from the added directory,
      despite `--help`'s wording suggesting otherwise (confirmed by a live
      test: a marker string in ``<dir>/CLAUDE.md`` never reached context).
    - ``--append-system-prompt <text>``: the reliable way to actually get the
      working-rules content into context, since file-based discovery from an
      added directory doesn't work. Reads ``<runtime_dir>/CLAUDE.md`` (the
      already-rendered working-rules doc `sync_runtime_docs` writes) and
      passes its content directly.
    - ``--settings <path>``: loads a JSON file whose top-level ``"hooks"`` key
      is honored (confirmed by a live test: a Stop hook in a `--settings`
      file actually fired). Points at the merged settings file
      `hooks.claude_settings_path_for_runtime` writes.

    All three are skipped gracefully if their source file doesn't exist yet
    (e.g. dry-run, or a `--secondary` join before any primary launch has
    provisioned the runtime)."""
    del repo  # cwd is set separately; claude has no -C-style flag
    cmd = ["claude", "--permission-mode", PERMISSION_MODE_BY_ACCESS[access_mode]]
    if runtime_dir is not None:
        cmd += ["--add-dir", str(runtime_dir)]
        agents_path = context_docs.target_path(runtime_dir, "claude")
        if agents_path.is_file():
            rules_text = agents_path.read_text().strip()
            if rules_text:
                cmd += ["--append-system-prompt", rules_text]
        settings_path = hooks.claude_settings_path_for_runtime(runtime_dir)
        if settings_path.is_file():
            cmd += ["--settings", str(settings_path)]
    cmd += passthrough
    return cmd


def build_command(
    engine: Engine,
    repo: Path,
    access_mode: AccessMode,
    passthrough: list[str] | None = None,
    runtime_dir: Path | None = None,
) -> list[str]:
    passthrough = passthrough or []
    if engine == "codex":
        return build_codex_command(repo, access_mode, passthrough)
    return build_claude_command(repo, access_mode, passthrough, runtime_dir=runtime_dir)


def env_overrides(engine: Engine, rctx: RuntimeContext) -> dict[str, str]:
    if engine == "codex":
        return {"CODEX_HOME": str(rctx.runtime_dir)}
    return {}


def _project_sandbox_write(repo: Path) -> list[str]:
    """`.cdx/project.json` "sandbox_write": extra absolute write roots for --safe Claude sessions (project data dirs)."""
    try:
        v = project.load_project(repo).get("sandbox_write")
    except (OSError, ValueError):
        return []
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


@dataclass(frozen=True)
class LaunchPlan:
    engine: Engine
    access_mode: AccessMode
    repo: Path
    runtime: RuntimeContext
    command: tuple[str, ...]
    cwd: Path
    env_overrides: dict[str, str]


@dataclass(frozen=True)
class PrepareResult:
    plan: LaunchPlan | None
    lock_acquired: bool
    lock_handle: session.LockHandle | None
    diagnosis: session.SessionDiagnosis | None
    link_decisions: tuple[skills.LinkDecision, ...]
    doc_sync: context_docs.SyncResult | None
    hook_install: hooks.HookInstallResult | None
    stale_worktrees: tuple[str, ...] = ()
    hygiene_line: str = ""


def prepare_launch(
    config: Config,
    repo: Path,
    engine: Engine = "codex",
    access_mode: AccessMode = "safe",
    passthrough: list[str] | None = None,
    skill_allowlist: frozenset[str] = frozenset(),
    dry_run: bool = False,
    secondary: bool = False,
    token_saver: bool = False,
) -> PrepareResult:
    """Provision the runtime, acquire the session lock, link skills, and sync
    hooks/docs -- everything short of actually exec'ing the engine binary.
    Diagnose-only when the lock can't be acquired (mirrors `session.py`'s
    default: never implicitly kill anything).

    `secondary=True` is for deliberately opening a second concurrent window
    into a repo that already has a live session (e.g. a second terminal) --
    it joins the already-provisioned runtime directly, skipping the exclusive
    lock (so it's never treated as a conflict requiring --cancel-active) and
    skipping skills/hooks/doc re-sync (so it never concurrently rewrites
    files the live primary session might be reading mid-session). Use
    --cancel-active instead when the goal is actually replacing a dead/stuck
    session, not joining a live one."""
    resolved_repo = repo_root(repo)
    rctx = provision_runtime(config, resolved_repo, access_mode=access_mode, engine=engine, dry_run=dry_run)

    # Read-only checks; never change the launch directory.
    stale = tuple(hygiene.stale_checkout_worktrees(resolved_repo))
    if stale:
        print("WARNING: " + hygiene.stale_warning_line(list(stale)), file=sys.stderr)
    hygiene_line = ""
    try:
        hygiene_line = hygiene.summary_line(hygiene.build_report(resolved_repo))
    except Exception:  # hygiene must never block a launch
        hygiene_line = ""
    if hygiene_line:
        print(hygiene_line, file=sys.stderr)

    lock_handle: session.LockHandle | None = None
    if secondary or dry_run:
        diagnosis = session.diagnose_session(resolved_repo, rctx.runtime_dir)
        lock_acquired = False
        plan_allowed = True
    else:
        acquisition = session.try_acquire(resolved_repo, rctx.runtime_dir)
        lock_handle = acquisition.handle
        lock_acquired = lock_handle is not None
        diagnosis = acquisition.diagnosis
        plan_allowed = lock_acquired

    link_decisions: tuple[skills.LinkDecision, ...] = ()
    doc_sync: context_docs.SyncResult | None = None
    hook_install: hooks.HookInstallResult | None = None
    if not dry_run and not secondary and plan_allowed:
        link_decisions = tuple(
            skills.link_all_skill_roots(
                config, resolved_repo, rctx.skills_dir, allowlist=skill_allowlist, engine=engine
            )
        )
        for decision in link_decisions:
            if decision.action != "blocked" or decision.audit is None:
                continue
            crit = next((f for f in decision.audit.findings if f.severity == "critical"), None)
            if crit is not None:
                print(
                    f"cdx-agent: blocked skill {decision.skill_name} ({crit.rule_name} line {crit.line_no})",
                    file=sys.stderr,
                )
        working_rules = context_docs.load_working_rules_template(config)
        rendered_rules = context_docs.render_working_rules(working_rules, token_saver=token_saver)
        rendered_rules = context_docs.filter_global_sections(
            rendered_rules, context_docs.load_exclude_sections(resolved_repo)
        )
        if stale:
            rendered_rules = rendered_rules.rstrip("\n") + "\n\n" + hygiene.stale_warning_line(list(stale)) + "\n"
        doc_sync = context_docs.sync_runtime_docs(rctx.runtime_dir, rendered_rules, engine=engine)
        hook_install = hooks.install_hooks_for_runtime(config, rctx.runtime_dir, engine=engine, access_mode=access_mode,
                                                    sandbox_write=_project_sandbox_write(resolved_repo))

    plan = None
    if plan_allowed:
        command = build_command(engine, resolved_repo, access_mode, passthrough, runtime_dir=rctx.runtime_dir)
        plan = LaunchPlan(
            engine=engine,
            access_mode=access_mode,
            repo=resolved_repo,
            runtime=rctx,
            command=tuple(command),
            cwd=resolved_repo,
            env_overrides=env_overrides(engine, rctx),
        )

    return PrepareResult(
        plan=plan,
        lock_acquired=lock_acquired,
        lock_handle=lock_handle,
        diagnosis=diagnosis,
        link_decisions=link_decisions,
        doc_sync=doc_sync,
        hook_install=hook_install,
        stale_worktrees=stale,
        hygiene_line=hygiene_line,
    )


@dataclass(frozen=True)
class LaunchOutcome:
    prepare: PrepareResult
    exit_code: int | None


def start_trash_maintenance(popen=subprocess.Popen) -> bool:
    """Decision Q2 (2026-11-20): drain staging and purge expired, non-critical trash at each launch, detached.

    Critical (protected-origin) entries are never auto-purged. Skipped when CDX_TRASH_AUTO=0 or when no trash index
    exists yet. Output goes to <index dir>/maintenance.log; failures never affect the launch.
    """
    if os.environ.get("CDX_TRASH_AUTO", "1") == "0":
        return False
    try:
        from cdx_agent import trash
        index = Path(trash.index_path())
        if not index.is_file():
            return False
        log = open(index.parent / "maintenance.log", "a")
        script = ("import sys; from cdx_agent import trash; "
                  "print('purged', trash.purge()); "
                  "import os; d=trash.DEFAULT_DEST; print('drained', trash.drain(d) if os.path.isdir(d) else 'skip')")
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
        popen([sys.executable, "-c", script], stdout=log, stderr=log, stdin=subprocess.DEVNULL,
              start_new_session=True, env=env)
        return True
    except Exception:
        return False


def launch(
    config: Config,
    repo: Path,
    engine: Engine = "codex",
    access_mode: AccessMode = "safe",
    passthrough: list[str] | None = None,
    skill_allowlist: frozenset[str] = frozenset(),
    dry_run: bool = False,
    secondary: bool = False,
    token_saver: bool = False,
) -> LaunchOutcome:
    prepare = prepare_launch(
        config,
        repo,
        engine=engine,
        access_mode=access_mode,
        passthrough=passthrough,
        skill_allowlist=skill_allowlist,
        dry_run=dry_run,
        secondary=secondary,
        token_saver=token_saver,
    )
    if prepare.plan is None:
        return LaunchOutcome(prepare=prepare, exit_code=None)
    if dry_run:
        return LaunchOutcome(prepare=prepare, exit_code=0)

    env = {**os.environ, **prepare.plan.env_overrides}
    start_trash_maintenance()
    try:
        result = subprocess.run(list(prepare.plan.command), cwd=str(prepare.plan.cwd), env=env)
        exit_code = result.returncode
    finally:
        if prepare.lock_handle is not None:
            session.release_lock(prepare.lock_handle)
    return LaunchOutcome(prepare=prepare, exit_code=exit_code)


def sync_docs_for_repo(
    config: Config, repo: Path, engine: Engine = "codex", adopt: bool = False, force: bool = False
) -> context_docs.SyncResult:
    """Standalone entry point for `cdx-agent sync-docs --repo .` -- lets a
    plain Claude Code (or Codex) session pick up the same generated
    AGENTS.md/CLAUDE.md without going through a special launch mode."""
    template = context_docs.load_repo_template(config)
    return context_docs.sync_repo_docs(repo_root(repo), template, engine=engine, adopt=adopt, force=force)


# --- CLI commands --------------------------------------------------------------------


RESUME_FLAGS = ("--resume", "-r", "--continue", "-c")


def has_resume_flag(passthrough: list[str]) -> bool:
    """True if the engine args already ask Claude to resume/continue a session."""
    return any(tok in RESUME_FLAGS or tok.startswith("--resume=") for tok in passthrough)


def report_background_tasks(account_home: Path, repo: Path, session_id: str | None, drain_s: float, dry_run: bool) -> None:
    """Before cancelling a live Claude session: list its background agents/shells that are still
    writing output (they stop when the session process is cancelled; their transcripts are kept),
    and optionally wait up to `drain_s` seconds for them to finish first."""
    if session_id is None:
        found = session.latest_claude_session(account_home, repo)
        session_id = found[0] if found else None
    if session_id is None:
        return
    task_dirs = session.claude_task_dirs(account_home, repo, session_id)
    active = session.running_background_tasks(task_dirs)
    if not active:
        print(f"  session {session_id}: no background agents/shells active.", file=sys.stderr)
        return
    window = int(session.TASK_ACTIVE_WINDOW_SECONDS)
    print(f"  session {session_id}: {len(active)} background agent/shell task(s) wrote output in the last {window} s:", file=sys.stderr)
    for f in active[:12]:
        print(f"    {f.name}", file=sys.stderr)
    if drain_s > 0 and not dry_run:
        print(f"  --drain: waiting up to {drain_s:.0f} s for them to finish ...", file=sys.stderr)
        active = session.drain_background_tasks(task_dirs, drain_s)
        print(f"  --drain: {len(active)} still active.", file=sys.stderr)
    if active:
        print(
            "  Cancelling stops these; their transcripts stay on disk. After --takeover (which resumes this "
            "conversation) they can be resumed from the new session by messaging each agent id.",
            file=sys.stderr,
        )


def command_launch(args) -> int:
    """`cdx-agent launch` -- the bash daily driver's replacement front door.
    Diagnose-only on a lock conflict unless --cancel-active or --takeover is explicitly
    passed (never implicitly kills anything, per session.py's design).
    --takeover = --cancel-active + relaunch Claude with `--resume <latest session of this repo>`, so the
    cancelled conversation carries over (its interrupted background agents can then be resumed).
    --drain SECONDS waits for the old session's background tasks to finish before cancelling."""
    cfg = load_config(getattr(args, "config", None))
    repo = Path(args.repo)
    if getattr(args, "worktree", None):
        repo = hygiene.resolve_worktree(repo, args.worktree)
    access_mode: AccessMode = "full" if args.full else "safe" if args.safe else cfg.default_access_mode
    passthrough = list(args.passthrough or [])
    if "--" in passthrough:
        # Drop only the first separator; later "--" tokens are payload meant
        # for the engine binary (e.g. to end ITS option parsing).
        passthrough.remove("--")
    passthrough = translate_passthrough_for_engine(args.engine, passthrough)
    secondary = getattr(args, "secondary", False)
    token_saver = getattr(args, "token_saver", False)
    takeover = bool(getattr(args, "takeover", False))
    drain_s = float(getattr(args, "drain", 0.0) or 0.0)

    takeover_session: str | None = None
    if takeover:
        if args.engine == "claude":
            found = session.latest_claude_session(cfg.account_home, repo_root(repo))
            if found is None:
                print("takeover: no Claude transcript found for this repo; a fresh session will start.", file=sys.stderr)
            elif has_resume_flag(passthrough):
                print("takeover: engine args already resume a session; leaving them unchanged.", file=sys.stderr)
            else:
                takeover_session = found[0]
                passthrough = ["--resume", takeover_session, *passthrough]
                print(f"takeover: will resume Claude session {takeover_session}", file=sys.stderr)
        else:
            print("takeover: resuming is Claude-only; for codex it behaves like --cancel-active.", file=sys.stderr)

    outcome = launch(
        cfg,
        repo,
        engine=args.engine,
        access_mode=access_mode,
        passthrough=passthrough,
        dry_run=args.dry_run,
        secondary=secondary,
        token_saver=token_saver,
    )

    if outcome.prepare.plan is None:
        diagnosis = outcome.prepare.diagnosis
        print(f"Another {args.engine} session appears active for this repo.", file=sys.stderr)
        if diagnosis is not None:
            print(f"  runtime_dir={diagnosis.runtime_dir}", file=sys.stderr)
            print(
                f"  lock_pid={diagnosis.lock_pid} alive={diagnosis.owner_alive} verified={diagnosis.owner_verified}",
                file=sys.stderr,
            )
        if not (args.cancel_active or takeover):
            print(
                "Pass --cancel-active to diagnose-and-cancel a stale/conflicting session, or --takeover to cancel it "
                "and resume its conversation (claude).",
                file=sys.stderr,
            )
            return 1

        resolved_repo = repo_root(repo)
        if args.engine == "claude" and diagnosis is not None and diagnosis.owner_alive:
            report_background_tasks(cfg.account_home, resolved_repo, takeover_session, drain_s, args.dry_run)
            if not takeover:
                print("  Tip: --takeover cancels and then resumes this same conversation.", file=sys.stderr)
        rctx_runtime_dir = provision_runtime(
            cfg, resolved_repo, access_mode=access_mode, engine=args.engine, dry_run=True
        ).runtime_dir
        cancel_result = session.cancel_session(resolved_repo, rctx_runtime_dir, dry_run=args.dry_run)
        print(f"cancel action={cancel_result.action}", file=sys.stderr)
        if cancel_result.detail:
            print(f"  {cancel_result.detail}", file=sys.stderr)
        if cancel_result.action == "refused" or args.dry_run:
            return 1

        outcome = launch(
            cfg,
            repo,
            engine=args.engine,
            access_mode=access_mode,
            passthrough=passthrough,
            dry_run=args.dry_run,
            secondary=secondary,
            token_saver=token_saver,
        )
        if outcome.prepare.plan is None:
            print("Still could not acquire the session lock after cancellation.", file=sys.stderr)
            return 1

    plan = outcome.prepare.plan
    print(f"ENGINE={plan.engine}")
    print(f"ACCESS_MODE={plan.access_mode}")
    print(f"REPO={plan.repo}")
    print(f"RUNTIME_DIR={plan.runtime.runtime_dir}")
    if args.dry_run:
        print("DRY_RUN_COMMAND=" + " ".join(plan.command))
        return 0
    return outcome.exit_code if outcome.exit_code is not None else 1


def command_sync_docs(args) -> int:
    cfg = load_config(getattr(args, "config", None))
    result = sync_docs_for_repo(cfg, Path(args.repo), engine=args.engine, adopt=args.adopt, force=args.force)
    print(f"{result.action}: {result.path}")
    if result.detail:
        print(result.detail)
    return 1 if result.action == "refused_hand_written" else 0


__all__ = [
    "CODEX_APPROVAL",
    "DEFAULT_ENGINE",
    "PERMISSION_MODE_BY_ACCESS",
    "SANDBOX_BY_ACCESS",
    "LaunchOutcome",
    "LaunchPlan",
    "PrepareResult",
    "build_claude_command",
    "build_codex_command",
    "build_command",
    "command_launch",
    "command_sync_docs",
    "env_overrides",
    "launch",
    "prepare_launch",
    "sync_docs_for_repo",
    "translate_passthrough_for_engine",
]
