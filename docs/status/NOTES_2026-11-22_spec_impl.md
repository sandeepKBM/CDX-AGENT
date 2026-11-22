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

## Open (needs the user or a live session)
- `--safe` sandbox ships `enabled: false` (`site/base/claude_sandbox_safe.json`). Live test in one session started with
  `CDX_SAFE_SANDBOX=1 cdx-agent --claude --safe`: write to codex_tools refused, rm under denyWrite refused, `--gpu-pick --status`
  and `git fetch` work. Then set enabled true and deploy.
- `--run` acceptance on a real hold (needs the user's go-ahead; no Slurm was used).
- Agent questions: exp archive `regenerable` fixed to yes; new ASSUMED bounds only warn in `exp run`; node release uses a
  plain copy (not coldbank put); junk session-reference grep covers the main checkout only; `/tmp/$USER/jobs` local scratch default.
- `tests/test_move_verify.py::test_verify_catches_extra_files_and_same_size_content_change` fails intermittently in full runs.
- cli.py still has `_dg_*` parsers; `Mode.deprecated` unused; `record_usage_snapshot` still calls `ccusage codex` (guarded).
- N1/N2 project skills and the bimanual AGENTS.md storage section: work order for the bimanual session.
- Merged agent worktrees under `.claude/worktrees/`: `cdx-agent --junk scan` lists them as tier a.
