# Handoff: implementing the 2026-11-20 cdx-agent spec (all phases, user: "keep going, complete all")

Plan (approved): ~/.claude/plans/silly-zooming-crab-of-b4a98a3b-56de-4a81-a10e-acad1dddbb34.md
Rulings: docs/status/decisions.md. Spec: /common/users/ss5772/downloads/bimanual/2026-11-20_cdx_agent_design/

## Done in main (not yet deployed unless noted)
- Phase 0: tools/ in git, site/ own local repo, deploy.py (`cdx-agent --deploy`), values ledger
  (live file /common/users/ss5772/.cdx/values.jsonl seeded, 9 rows), decisions ledger, F22.
- Phase 1 code: output_cap.py PostToolUse (WP2) + output_filter no-op; delete_guard.py in pre_tool_use_policy (WP3 hook);
  delete_policy.py, inuse.py (ancestor cmdline self-match fixed), trash.py, audit.py, hygiene F4, move_verify --finish,
  skills audit F23 + waivers + blocked-skill line, bash modes --deploy/--value/--rm/--trash/--inuse,
  site/: rule texts §4.9 rewritten, 10 Compute/Slurm footers removed, delete_policy.json + skill_waivers.json.
- Agents' sandboxes refuse git in worktrees: copy their files into main and commit from the main session.
  Worktrees branch from 312ffc3 (old origin/main): tell agents to `git merge --no-edit main` first.

## Next
1. Phase 1 deploy: full suite green -> commit -> `mkdir -p /common/home/ss5772/.cdx/trash_staging
   /common/users/ss5772/.cdx/trash_staging /data/local/ss5772/trash` -> `cdx-agent --deploy`.
   Live checks need a NEW session (settings are generated at launch): `touch x` prompts in --safe; long output capped with
   log path; `rm -r somedir` denied with --rm hint. Retire skills Q5/Q8 via `cdx-agent --rm` (live copies).
   Launch-time drain+purge (Q2) in launch.py not done yet.
   Note: the delete guard imports cdx_agent from the main checkout src (dev tree) -> a broken src fails closed (denies deletes).
2. Phase 2 (WP1 run.py/gpu_lock/cpu_job/notify, WP5 modes registry/dispatch/site.json, Q6 sbatch deny), Phase 3, Phase 4
   per plan. Delegate to Sonnet implement agents with exclusive files; review + commit in main.
