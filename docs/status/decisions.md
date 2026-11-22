# CDX-AGENT decision ledger

User rulings only (an agent's message is never a ruling). Newest first. Dates carry the year.
Source design: `/common/users/ss5772/downloads/bimanual/2026-11-20_cdx_agent_design/` (01 spec, 02 study report);
plan: `~/.claude/plans/silly-zooming-crab-of-b4a98a3b-56de-4a81-a10e-acad1dddbb34.md`.

## Open
- none

## Rulings
| Date | # | Ruling (user's words where given) | Replaces |
|---|---|---|---|
| 2026-11-22 | Q4 | `--safe` sandbox enabled after the live test (results in `site/base/claude_sandbox_safe.json` `_comment`), with the base-rule line: in `--safe`, run ssh/srun/sbatch/cdx-agent as plain commands. | Q4 "ships disabled until live test" |
| 2026-11-22 | scope | Implement all phases (0-4) of the spec, in order. | - |
| 2026-11-22 | Q9 | Safety-math verifier runs on Opus: new agent type `safety-review` {opus, sonnet}; `review` stays Sonnet. | N7 model open |
| 2026-11-22 | Q8 | Retire `openpi-training-diagnosis` (both copies), `repo-impact-analysis`, `checkpoint-selection` (via `--rm`). | - |
| 2026-11-22 | Q7 | Code in CDX-AGENT git (`tools/`); personal/site files in gitignored `site/` (own local git, no remote). Ends `.bak.<date>` rule. | `.bak.<date>_<tag>` before live edits |
| 2026-11-22 | Q1 | After a checksum-verified second copy, a protected source is tier (b): trashed (7-day TTL, restorable). | user `!` line for every delete |
| 2026-11-20 | spec | No forks; upgrade `cdx-agent --brief` with a safety-math focus (formulas + code, no conclusions) for a fresh verifier. | N7 `context: fork` |
| 2026-11-20 | Q2 | Drain/purge automatic, except critical (tier-1 / protected-origin) entries. | - |
| 2026-11-20 | Q3 | ASSUMED/tunable values in a machine-written ledger with change reasons; agents update it, code reads it. | numbers hard-coded |
| 2026-11-20 | Q4 | `--safe` gets the Claude Code sandbox (ssh/srun excluded as needed). | text-only hardware rule |
| 2026-11-20 | Q5 | Remove Codex-only modes and the 9 `dg` modes (+ `multi-root-context-map`). | - |
| 2026-11-20 | Q6 | Raw `sbatch`/`srun` becomes a deny once standard launchers exist. | advisory |
| 2026-11-20 | WP3 | Deletion tiers (a) allow-list delete+log, (b) trash, (c) user only; trash TTL 7 days. | literal `rm -rf` block |
