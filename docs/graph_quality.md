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

**E3 — real import resolution (2026-07-02).** `_reverse_import_index` (bare module name →
importing files) is ambiguous for same-named modules in different packages and is now kept
only as a fallback. `import_edges` on each `FileNode` resolves every `import`/`from ... import`
statement to a concrete file where possible — walking package roots (repo root, `src/` if
present), handling relative imports via `level`, and preferring the submodule form
(`from pkg import sub` → `pkg/sub.py`) over the package `__init__.py` when both exist.
`file_reverse_import_index` (file-path-keyed, only resolved edges) replaces the ambiguous
index as `--impact`'s primary source, with the bare-name index as a fallback for anything
that doesn't resolve (e.g. dynamic imports). `_config_edges` now verifies every regex-matched
path-looking string actually resolves to a real file (tried relative to the repo root and to
the referencing file's own directory) before keeping the edge, instead of keeping every match
unconditionally. See `tests/test_graph_import_resolution.py`.

**E5 — incremental/cached builds (2026-07-02).** `_scan_repository` now keys a per-repo cache
(`.codex_graph/.scan_cache.json`) by `(path, mtime, size, kind)`; an unchanged file's
previously-computed `FileNode` and parse errors are reused instead of re-parsed. The whole
cache is invalidated at once if `topic_hints` changed since tags depend on them. Confirmed on
a real 385-file repo (real_Cartpole): 2.45s cold → 0.43s warm, ~5.7x. See
`tests/test_graph_incremental.py`.

**E4 — Dockerfile support (2026-07-02).** `_classify_file` recognizes `Dockerfile`,
`Dockerfile.*`, and `*.dockerfile` and routes them through the existing generic textual scan
(tags + path_refs), so `CMD`/`ENTRYPOINT` lines referencing a real `.py`/`.sh` file are picked
up automatically by the existing path-ref regex with no dedicated parser. See
`test_classify_file_recognizes_dockerfile` in `tests/test_graph_scan_quality.py`.

**E6 — smarter entrypoint scoring (2026-07-02).** `_likely_entrypoints` now also discovers
project-declared entrypoints — `pyproject.toml [project.scripts]`, `setup.py`
`console_scripts` (best-effort regex, not exec/AST since setup.py can run arbitrary code),
Makefile targets, and CI `run:` steps (`.github/workflows/*.yml`, `.gitlab-ci.yml`) — and
weights a match 2x over the old filename-keyword-regex signal, with the reason recorded in a
new `declared_by` field. See `tests/test_graph_entrypoint_scoring.py`.

**E7 — task-relevance scoring (2026-07-02).** `_score_node_for_task` gained an optional
TF-IDF-style weighting: `_document_frequencies` counts, per task token, how many nodes contain
it; matches are weighted by a smoothed idf (`log((N+1)/(df+1)) + 1`) so a token appearing in
nearly every file (weak signal) contributes far less than one appearing in a handful (strong,
specific signal). Falls back to the original flat weighting when called without
`doc_freqs`/`total_nodes` (backward compatible). See `tests/test_graph_task_relevance.py`.

## Test coverage added this pass

- `tests/test_graph_scan_quality.py` — 16 tests: home-dir ancestor fix, truncation
  prioritization/reporting, topic-hint override/discovery/precedence, Dockerfile
  classification (E4).
- `tests/test_graph_import_resolution.py` — 15 tests (E3): absolute/relative import
  resolution, the same-named-module disambiguation regression, `--impact` output,
  `config_edges` resolution.
- `tests/test_graph_incremental.py` — 9 tests (E5): cache reuse, content-change detection,
  hint-change invalidation, corrupt-cache tolerance.
- `tests/test_graph_entrypoint_scoring.py` — 7 tests (E6): pyproject/setup.py/Makefile/CI
  discovery, declared-entrypoint outranking filename heuristics.
- `tests/test_graph_task_relevance.py` — 5 tests (E7): document-frequency counting, idf
  weighting outranking a common token, backward-compat flat fallback.
