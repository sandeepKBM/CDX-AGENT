# Handoff CDX-AGENT
Generated: 2026-11-22T03:09:49-05:00
Repo root: /common/home/ss5772/CDX-AGENT
Current: main @ 57c2501 docs/status: handoff notes for spec implementation
## Worktrees
(none)
## Slurm
queued/running:
  1331849 bim_hold RUNNING 2-23:54:30 rlab4 None
finished: (none)
## Local GPUs
- GPU 0: 9711 MiB, 94% util, owners: jhd79
- GPU 1: 9657 MiB, 95% util, owners: jhd79
- GPU 2: 2538 MiB, 1% util, owners: jhd79
- GPU 3: 7958 MiB, 70% util, owners: jhd79
- GPU 4: 10215 MiB, 93% util, owners: jhd79
- GPU 5: 8364 MiB, 99% util, owners: jhd79
- GPU 6: 5353 MiB, 97% util, owners: jhd79
- GPU 7: 8741 MiB, 97% util, owners: jhd79
## tmux sessions
(unavailable: command failed)
## Reports
(none)
## Open items
(none)
## Notes (agent-written interpretation)
# Handoff: implementing the 2026-11-20 cdx-agent spec

Plan (approved, all phases): ~/.claude/plans/silly-zooming-crab-of-b4a98a3b-56de-4a81-a10e-acad1dddbb34.md
Rulings: docs/status/decisions.md. Spec: /common/users/ss5772/downloads/bimanual/2026-11-20_cdx_agent_design/

## Done (commit 776eb64, site/ commit f201eec)
- tools/{bin,hooks} tracked; site/{base,skills/shared,skills/user} = own local git (gitignored, no remote).
- `python -m cdx_agent.deploy [--dry-run|--rev|--force|--allow-dirty|--skip-tests]` (not yet a bash mode).
  Dry run on live: 0 to install, 32 current, 0 drift.
- Hook tests use tools/hooks. Suite: 497 passed, 2 skipped. ruff is NOT installed (CI has it).

## Next (Phase 0 rest, then Phase 1)
1. Add `--deploy` and `--value` to bash mode list (tools/bin/cdx-agent ~line 2162 + case block ~2520), commit, deploy.
2. values.py + `--value get|set|list|history` on site/base/values.jsonl (seed rows in plan Phase 0 step 5).
3. Phase 1: WP2 output_cap.py (PostToolUse updatedToolOutput {stdout,stderr,interrupted,isImage}), WP3, WP6 1+3.
- Edit repo copies only; live goes through deploy. Live hooks are symlinked by every runtime.
- Use Sonnet implement agents (exclusive files, seeded with `cdx-agent --brief`) for nfs.py, stats.py, values.py.
## Resume
Base rules: /common/users/ss5772/codex_tools/base/AGENTS.md
Start a fresh session in /common/home/ss5772/CDX-AGENT and say: "Read /common/home/ss5772/CDX-AGENT/docs/status/2026-11-22_handoff_auto.md and continue from its open items."
