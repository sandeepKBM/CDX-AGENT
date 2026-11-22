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
