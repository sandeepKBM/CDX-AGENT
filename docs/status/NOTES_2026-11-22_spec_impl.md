# Handoff: implementing the 2026-11-20 cdx-agent spec (all phases, user: "keep going, complete all")

Spec: `/common/users/ss5772/downloads/bimanual/2026-11-20_cdx_agent_design/01_cdx_agent_implementation_spec.md`.
Plan: `~/.claude/plans/silly-zooming-crab-of-b4a98a3b-56de-4a81-a10e-acad1dddbb34.md`. Rulings: `docs/status/decisions.md`.

## Done in main (CDX-AGENT) and site/ (local git)
- Phase 0-1 (deployed 2026-11-22 morning): deploy, values ledger, delete tiers + trash + audit, output cap, skill audit fixes.
- Phase 2: `--run` (WP1, run.py/manifest.py), gpu_lock `--n 0/--chdir/--env`, cpu_job cap refusal, notify `--from-start/--grace-s`,
  mode registry + unknown-flag exit 2 (WP5), `--site-facts`, F20 site.json reads, Q6 raw sbatch/srun/salloc hook deny
  (`tools/hooks/slurm_guard.py`, escape `CDX_RAW_SLURM=1`).
- Phase 3: `.cdx` conventions, `--suite`, `--check` (WP4), F11 long-suite guard over ssh/srun; full `--validate-skills`,
  `--skills-eval --list`; skills experiment-protocol, claim-audit, falsify-and-bisect, decision-ledger, safety-math-review
  (no fork), storage (merged), slurm-gpu-allocation trimmed to 27 lines; evals for the new skills; `--brief --focus safety-math`.
- Phase 4: `--junk scan/apply`, `--node setup/status/release` (WP7); `--exp` + runlog + expconfig (WP8); bash front end
  2623 -> 1234 lines, Q5 modes removed, teardown/hash-repro in Python, install-hooks -> `.claude/settings.json`, lock host +
  start time (WP9); poll guard rewrite, agent gate subagent_type + concurrency ask, SubagentStop claims + `--verify-log`,
  F15 matcher `Bash|apply_patch`, `.cdx/hardware.json` ask, `--safe` sandbox block (WP10).

## Closed 2026-11-22 (tested, not flagged)
- `--safe` sandbox: live-tested headless (deny writes/unlink, allowWrite, deny-inside-allow, network allowlist, plain
  excluded commands work, piped ones do not), enabled by the user, deployed. A sandbox added to a running session's settings
  file did not apply mid-session (headless, 15 s), so running sessions keep their start settings.
- `--run` acceptance: Slurm job 1376133 on ilab3 (1 CPU, 1 GB, `hostname`), exit 0, DONE.json collected, manifest
  `succeeded`. Local runs use the HOT tier (`/data/local/$USER/jobs`) and export `CDX_JOB_SCRATCH`/`TMPDIR`.
- Intermittent tests: move_verify (dir mtime-only itemize line was a false FAIL in the verifier itself) and notify CLI test
  (startup race; uses `--from-offset`).
- exp: archive `--regenerable` required; new ASSUMED bounds refuse a scored run (smoke warns); YAML 1.2 booleans (`on:` key).
- Registry-driven dispatch: a registered python mode without a bash case dispatches to its module (no launch fallthrough).
- `--model-diff`/`--lint` (N1 backing); dg python removed; ccusage codex snapshot removed; merged agent worktrees removed
  with `git worktree remove` via `--junk apply`, merged branches deleted.
- Lint: ruff 0.17.6 + shellcheck 0.11.0 in `/data/local/ss5772/tools/lint/bin`; classic default rules clean except 12 E741
  (`l` names, older code); shellcheck -S warning: 5 warnings (unused vars of no-op compat flags, one SC2155).
- N1/N2 + bimanual rule text: work order `docs/status/2026-11-22_bimanual_work_order.md` (copy in
  `/common/users/ss5772/downloads/bimanual/2026-11-22_cdx_agent_work_order/`); MjSpec `alt` degree fact verified.

## Open
- Bimanual work order items (other repo).
- Two unmerged/dirty agent worktrees kept (tier b, under 30 days): agent-a0d9aabf8e03c7b06, agent-aefda0a9896553ad6.
