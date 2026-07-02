# Graph/context-pack quality — status and backlog

Tracks Workstream E from the CDX-AGENT revamp plan (`.claude/plans/i-want-you-to-tingly-iverson.md`
on the box this was authored on). Ranked E1→E7 by value/effort; this file records what's
actually landed in `src/cdx_agent/graph.py` vs. what's still backlog.

## Landed

**Consistency fix (not one of E1-E7, but touched while working in this area):**
`_is_home_like_dir` previously only matched the home directory by exact equality
(mirroring the same bug bash and `config.py` had before their own fixes). It now does a
proper ancestor check — a subdirectory of home, or a symlink resolving into it, is also
refused for a full-tree scan unless `--force-home-scan` is passed. See
`test_is_home_like_dir_catches_subdirs` in `tests/test_graph_scan_quality.py`.

**E1 — report + prioritize truncation.** `_iter_files` used to silently cap at
`max_files` with only a boolean `scan_truncated` flag and no indication of *which* files
were dropped or any prioritization of which ones survived. Now:
- Candidates are collected up to a `SCAN_HARD_CEILING` (200,000 files) — a real bound so
  prioritization doesn't turn into an unbounded walk on a pathological tree — then, only
  if the file count exceeds `max_files`, ranked by `_file_priority_key` (entrypoint-like
  filenames first, then shallower paths, then most-recently-modified) before truncating.
- `repo_graph.json` gains `scan_dropped_count` and `scan_dropped_sample` (up to 50 paths).
- `context_pack.md`'s "Graph notes" section now names a sample of what was dropped, not
  just that truncation happened.

**E2 — de-hardcode `TOPIC_HINTS`.** Tagging previously matched only the fixed 8-word
robotics-flavored tuple (`policy`, `controller`, `openpi`, `mujoco`, ...) baked into the
module, with no way to use this tool meaningfully on a non-robotics repo. Now
`resolve_topic_hints(repo)`:
1. Uses an explicit `topic_hints: [...]` list in `.codex_graph/workspace.yaml` if present
   (fully replaces the defaults — an intentional override, not a merge).
2. Otherwise merges the shipped `TOPIC_HINTS` defaults with a cheap discovery pass over
   the repo's own `pyproject.toml` (`[project].keywords`) and top-level local package
   names (`repo/*/`, `repo/src/*/` with an `__init__.py`) — so a repo with zero
   robotics-specific vocabulary still gets *some* non-generic tags.
`build_graph`'s output records the `topic_hints` actually used, for transparency.
`WORKSPACE_DETECT_PACKAGES` (the multi-repo dependency-detection seed list) was
deliberately **not** touched here: `_workspace_detect_dependencies` already merges real
discovered imports (`import_names`) and `_project_dependency_hints(repo)` (parsed from
`pyproject.toml`/`requirements.txt`) with the constant seed list, so it was already
discovery-driven rather than hardcoded-only — the constant is just extra seed candidates,
not the sole source, and touching it carried more risk than the marginal value justified
given the E1/E2 effort budget for this pass.

## Backlog (not started this pass — ranked by original value/effort estimate)

- **E3 — real import resolution.** `_reverse_import_index` still maps bare module names
  (not resolved to a specific file) to importing files, ambiguous for same-named modules
  in different packages; `_config_edges` still keeps every regex-matched path-looking
  string without verifying it resolves to a real file. This is the highest remaining
  value item — it directly affects `--impact`/context-pack accuracy — and the highest
  effort (needs package-root walking + relative/absolute import resolution per file, not
  just per workspace-dependency as `_resolve_dependency_record` already does).
- **E5 — incremental/cached builds.** Every `--graph`/`--context` invocation does a full
  rescan. A `(path, mtime, size)`-keyed cache in `.codex_graph/` would let unchanged files
  skip re-parsing. Matters more once E3 makes per-file analysis heavier.
- **E4 — broader language coverage.** Still python/shell/yaml/json/toml only. Dockerfile
  support (cheap, high-signal for entrypoint detection) is the easiest next addition;
  broader AST-level support for other languages is a much bigger lift (tree-sitter or
  similar) and should be scoped separately once it's clear which languages actually
  matter across the user's working repos.
- **E6 — smarter entrypoint scoring.** `_likely_entrypoints` still scores purely by
  filename regex + presence of a `main` function + hardcoded domain tags. Weighting
  `pyproject.toml [project.scripts]`/`setup.py console_scripts`/Makefile/CI `run:` steps
  as stronger signals is a contained, low-risk follow-up.
- **E7 — task-relevance scoring.** `_score_node_for_task`'s naive token-overlap could get
  a cheap TF-IDF-style improvement (penalize tokens common across most files, reward rare
  discriminating tokens) using only `collections.Counter` — no new dependency. Lowest
  priority; do after the above if time permits.

## Test coverage added this pass

`tests/test_graph_scan_quality.py` — 14 tests covering the home-dir ancestor fix,
truncation prioritization/reporting, and topic-hint override/discovery/precedence.
