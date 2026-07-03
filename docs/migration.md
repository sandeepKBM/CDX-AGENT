# Bash → Python migration checklist

Tracks the migration of `/common/users/ss5772/bin/cdx-agent` (bash, ~97 functions) into
the `cdx_agent` Python package, per the CDX-AGENT revamp plan. One row per bash function.

Columns:
- **Ported** — Python equivalent exists and has test coverage.
- **Target module** — where the logic lands in `src/cdx_agent/`.
- **Parity test** — name of the test that verifies bash/Python behavioral parity (during
  the transition window only; deleted once bash no longer contains the logic).
- **Bash delegates** — bash's own function has been replaced with a call into the Python
  CLI (`cdx-agent ...` / `python3 -m cdx_agent ...`) instead of containing the original logic.

Update this file at the end of every migration step (see plan Workstream B / Phase
sequencing in `.claude/plans/i-want-you-to-tingly-iverson.md` on the box this was authored
on — kept here as the durable in-repo record).

## Step 1 — pure/no-side-effect helpers → `config.py` / shared utils

| Bash function | Ported | Target module | Parity test | Bash delegates |
|---|---|---|---|---|
| `timestamp` | Y | config.py::timestamp | — | N |
| `hostname_short` | Y | config.py::hostname_short | — | N |
| `have_cmd` | N | (use stdlib `shutil.which` directly at call sites, no wrapper needed) | — | N |
| `current_dir` | N | (use `Path.cwd()` directly at call sites) | — | N |
| `cdx_is_dry_run` | N | deferred to launch.py/session.py (Phase 2/4, needs a shared `--dry-run` CLI flag first) | — | N |
| `cdx_abs_path` | Y | config.py::abs_path | — | N |
| `cdx_sanitize_name` | Y | config.py::sanitize_name | — | N |
| `cdx_validate_workspace_name` | N | workspace_mirror.py (Step 7) | — | N |
| `is_git_repo` | Y | config.py::is_git_repo | — | N |
| `repo_root` | Y | config.py::repo_root | — | N |
| `looks_like_project_dir` | Y | config.py::looks_like_project_dir | — | N |
| `is_home_like_dir` | Y | config.py::is_home_like_dir — **fix included**: real ancestor/prefix check (`candidate in resolved.parents`), not bash's exact-match-only comparison | test_config.py::test_is_home_like_dir_catches_subdirs | N |
| `repo_name` | Y | config.py::repo_name | — | N |
| `repo_slug` | Y | config.py::repo_slug | test_config.py::test_repo_slug_is_stable_and_path_derived | N |
| `backup_path` | Y | config.py::backup_path | test_config.py::test_backup_path_copies_and_timestamps | N |
| `confirm_or_die` | N | deferred to cli.py (Phase 3/4, needs an interactive-prompt layer) | — | N |
| `guarded_graph_target_dir` | N | graph.py already has an equivalent (`resolve_repo_root` + home-dir guard); reconcile with config.py's `is_home_like_dir` in Workstream E | — | N |

## Step 2 — runtime provisioning → `runtime.py` (A1, A7, A2 land here)

| Bash function | Ported | Target module | Parity test | Bash delegates |
|---|---|---|---|---|
| `runtime_codex_home` | Y | runtime.py::runtime_home — **fix A1**: path folds in `access_mode`+`engine` | test_runtime.py::test_full_and_safe_get_isolated_runtime_dirs | N |
| `legacy_codex_home` | Y | runtime.py::legacy_runtime_home (kept only for migration detection) | test_runtime.py::test_migrate_legacy_runtime_copies_into_full_slot | N |
| `current_codex_home` | Y | runtime.py::runtime_context — **removes the dead-code full/safe aliasing bug**, plus new `migrate_legacy_runtime` one-time migration | test_runtime.py::test_migrate_legacy_runtime_copies_into_full_slot | N |
| `ensure_tools_layout` | N | config.py::Config.directory_skeleton() defines the layout; actual `mkdir`ing deferred to Phase 6's `init-user` command | — | N |
| `ensure_repo_graph_pythonpath` | N | not needed in the Python package — `graph.py` is imported directly, no PYTHONPATH shell-out required | — | n/a |
| `extract_user_model_setting` | Y | runtime.py::_extract_toml_key (used inside `render_codex_config`) | test_runtime.py::test_config_resyncs_when_source_changes | N |
| `ensure_runtime_config` | Y | runtime.py::sync_runtime_config — **fix A7**: content-hash sync instead of write-if-absent | test_runtime.py::test_config_resyncs_when_source_changes | N |
| `copy_auth_if_needed` | Y | runtime.py::sync_runtime_auth — **fix A7**: same hash-sync mechanism | test_runtime.py::test_auth_resyncs_after_relogin | N |
| `ensure_runtime_agents` | N | context_docs.py (Phase 3) | — | N |
| `ensure_runtime_skills` | N | skills.py (Phase 3) | — | N |
| *(new)* `resync` | Y | runtime.py::resync — explicit force-resync escape hatch when auto-sync detects a conflict | test_runtime.py::test_resync_does_not_clobber_user_edited_runtime_config_without_warning | n/a |
| *(new)* `reap_stale_runtimes` | Y | runtime.py::reap_stale_runtimes — **new, fix A2**: reports age always, expires `*.stale.*` dirs holding `auth.json` past a configurable retention window, containment-checked delete | test_runtime.py::test_reap_stale_runtimes_respects_age_and_containment, test_reap_stale_runtimes_refuses_to_delete_outside_runtime_root | n/a |

## Step 3 — session lock/conflict/cancel → `session.py` (A3 lands here, highest test bar)

| Bash function | Ported | Target module | Parity test | Bash delegates |
|---|---|---|---|---|
| `ensure_runtime_lock` | Y | session.py::acquire_lock/try_acquire — **fix A3**: writes owning PID into lock file at acquisition | test_session.py::test_acquire_lock_writes_pid | N |
| `cdx_list_matching_session_processes` | Y (replaced) | session.py — **not ported as-is**: no more `ps\|grep` substring match; replaced by `is_lock_held_by`/`child_pids` PID verification | test_session.py::test_cancel_does_not_kill_unrelated_process_with_matching_cwd_string | N |
| `cdx_print_active_session_diagnosis` | Y | session.py::diagnose_session | test_session.py::test_diagnose_session_verifies_live_holder | N |
| `cdx_cancel_active_session` | Y | session.py::cancel_session — **fix A3**: PID-verified, TERM→verify→KILL, refuses to signal an unverified PID | test_session.py::test_cancel_kills_actual_lock_holder, test_cancel_refuses_to_signal_pid_that_no_longer_holds_the_lock | N |
| `cdx_handle_active_session_conflict` | Y | session.py::handle_conflict — **diagnose-only by default**, live cancellation requires explicit `mode="cancel"` | test_session.py::test_handle_conflict_defaults_to_diagnose_only, test_handle_conflict_cancel_mode_signals | N |
| `cdx_session_doctor` | Y | session.py::diagnose_session (doctor.py wraps this for CLI output, Phase 3+) | — | Y |
| `launch_log_dir` | Y | session.py::launch_log_dir | test_session.py::test_launch_log_dir_uses_sanitized_repo_name | N |
| `ensure_session_log_dir` | N | deferred to launch.py (Phase 4) — needs the per-launch session-state object | — | N |
| `sanitize_file_stem` | Y | config.py::sanitize_name (reused, not duplicated) | — | N |
| `raw_output_path_for_session` | Y | session.py::raw_output_path | — | N |
| `compressed_output_path_for_session` | Y | session.py::compressed_output_path | — | N |
| `write_token_saver_marker` | N | deferred to launch.py (Phase 4) | — | N |
| `session_root_candidates` | Y | session.py::session_root_candidates | test_session.py::test_session_root_candidates_dedupes | N |
| `detected_session_dirs` | Y | session.py::detected_session_dirs | — | N |
| `session_jsonl_files` | Y | session.py::session_jsonl_files — simplified via `rglob("*.jsonl")` instead of bash's manual maxdepth-8 path-pattern list | test_session.py::test_session_jsonl_files_and_recent_count | N |
| `recent_session_jsonl_count` | Y | session.py::recent_session_jsonl_count | test_session.py::test_session_jsonl_files_and_recent_count | N |
| `print_recent_session_file_table` | N | deferred to doctor.py (Phase 3+, presentation-only wrapper around the above) | — | N |

## Step 4 — skills linking + audit enforcement → `skills.py` (A4 lands here)

| Bash function | Ported | Target module | Parity test | Bash delegates |
|---|---|---|---|---|
| `cdx_collect_runtime_skill_roots` | Y | skills.py::collect_runtime_skill_roots (delegates to `config.repo_skill_roots`) | test_skills.py::test_collect_runtime_skill_roots_excludes_quarantine | N |
| `cdx_link_skill_root` | Y | skills.py::link_skill_root — **fix A4**: enforced audit gate at link time (was link-only for every root but quarantine) | test_skills.py::test_link_skill_root_blocks_critical_by_default, test_link_skill_root_allowlist_override_permits_link | N |
| `cdx_skill_root_status` | N | deferred to doctor.py (Phase 4+, presentation wrapper) | — | N |
| `run_validate_skills` | Y (partial) | skills.py::discover_skills covers discovery; CLI-facing `--validate-skills`/`--skills-list`/`--skills-audit` reporting deferred to cli.py (Phase 4+) | test_skills.py::test_discover_skills_finds_across_roots_and_dedupes_root_kinds | Y |
| `run_skills_list` | N | cli.py (Phase 4+, wraps skills.py::discover_skills) | — | Y |
| `run_skills_audit` | Y (partial) | skills.py::audit_skill_cached / audit_skill_text; CLI reporting deferred to cli.py | test_skills.py::test_audit_cache_invalidates_on_content_change | Y |
| `skill_validation_summary` | N | deferred to doctor.py (Phase 4+) | — | N |

## Step 5 — hooks + AGENTS.md/CLAUDE.md generation → `hooks.py` + `context_docs.py` (A6, D2, D3)

| Bash function | Ported | Target module | Parity test | Bash delegates |
|---|---|---|---|---|
| `default_repo_agents_content` | Y | context_docs.py::render | test_context_docs.py::test_render_substitutes_repo_name_and_carries_marker | N |
| `write_repo_agents` | Y | context_docs.py::sync_repo_docs — **fix D2**: generated-file marker required before touching a target again; refuses by default (stricter than bash's auto-append) unless `adopt=True`/`force=True`, engine-aware output filename (AGENTS.md vs CLAUDE.md) | test_context_docs.py::test_does_not_clobber_hand_written_claude_md | N |
| `render_runtime_agents` | Y | context_docs.py::load_canonical_template + render (runtime-dir variant deferred to launch.py, Phase 4) | — | N |
| `write_repo_hooks_json` | Y | hooks.py::write_hooks_json / build_hooks_payload — **fix A6/D3**: single source of truth wires all 4 scripts (was dropping `token_risk_warn.py`), engine-aware output location | test_hooks.py::test_generated_hooks_json_matches_example_template_script_set | N |
| `install_repo_hooks` | Y | hooks.py::install_hooks_for_repo | test_hooks.py::test_install_hooks_for_repo_end_to_end | N |
| `write_runtime_hooks_json` | Y | hooks.py::write_hooks_json (shared with repo path via hooks_locations_for_runtime) | — | N |
| `install_runtime_hooks` | Y | hooks.py::install_hooks_for_runtime — now engine-parameterized (D3), both codex and claude runtimes get isolated hook installs per the A1 runtime path scheme | test_hooks.py::test_install_hooks_for_runtime_both_engines_isolated | N |

## Step 6 — launch orchestration → `launch.py` (D1 completion)

| Bash function | Ported | Target module | Parity test | Bash delegates |
|---|---|---|---|---|
| `launch_codex` | Y | launch.py::launch/prepare_launch — **fix D1**: engine-parameterized (`build_codex_command`), plus new `build_claude_command`/`launch_claude` sibling sharing the same runtime/session/skills/hooks pipeline | test_launch.py::test_launch_invokes_stub_codex_binary_with_expected_env_and_cwd, test_launch_invokes_stub_claude_binary | Y |
| `graph_preflight` | N | deferred — needs graph.py wired in directly (Phase 5 territory once graph.py's own interfaces are finalized) | — | N |
| `run_repo_graph_build` | N | deferred to Phase 5 (calls graph.py directly, no more subprocess/PYTHONPATH shellout) | — | N |
| `run_repo_graph_context` | N | deferred to Phase 5 | — | N |
| `run_repo_graph_detect_deps` | N | deferred to Phase 5 | — | N |
| `run_repo_graph_init_workspace` | N | deferred to Phase 5 | — | N |
| `run_repo_graph_workspace_graph` | N | deferred to Phase 5 | — | N |
| `run_repo_graph_workspace_doctor` | N | deferred to Phase 5 | — | N |
| `write_launch_command` | N | deferred to cli.py (needs the session logdir wiring from session.py's Step 3 `ensure_session_log_dir` gap) | — | N |
| `finalize_launch_log` | N | deferred to cli.py | — | N |
| `record_launch_metadata` | N | deferred to cli.py | — | N |
| `record_usage_snapshot` | N | deferred to cli.py / token_tools.py | — | N |
| `review_repo` | N | deferred to cli.py | — | N |
| `run_init_repo` | N | deferred to cli.py | — | N |
| `run_graph_only` | N | deferred to cli.py | — | N |
| `run_install_hooks` | Y (equivalent) | hooks.py::install_hooks_for_repo (ported in Step 5, reused here) | test_hooks.py::test_install_hooks_for_repo_end_to_end | Y |
| `print_doctor` | N | deferred to doctor.py | — | N |
| *(new)* `sync_docs_for_repo` | Y | launch.py::sync_docs_for_repo — **D2 standalone entry point**: `cdx-agent sync-docs --repo .` for plain sessions with no special launch mode | test_launch.py::test_sync_docs_for_repo_writes_correct_filename_per_engine | n/a |
| `print_usage` | N | cli.py | — | N |

## Step 7 — dg-workspace mirroring → `workspace_mirror.py` (lowest risk, ported last)

| Bash function | Ported | Target module | Parity test | Bash delegates |
|---|---|---|---|---|
| `cdx_workspace_name_from_spec` | Y | workspace_mirror.py::workspace_name_from_spec | test_workspace_mirror.py::test_workspace_name_from_spec_uses_basename_for_file | N |
| `cdx_workspace_manifest_path` | Y | workspace_mirror.py::workspace_manifest_path | test_workspace_mirror.py::test_workspace_manifest_path_for_named_workspace | N |
| `cdx_workspace_entries` | Y | workspace_mirror.py::workspace_entries | test_workspace_mirror.py::test_workspace_entries_skips_blank_and_comment_lines | N |
| `cdx_workspace_mirror_path` | Y | workspace_mirror.py::workspace_mirror_path | — | N |
| `cdx_need_dg` | Y | workspace_mirror.py::need_dg / DG_INSTALL_INSTRUCTIONS — unchanged posture: no curl-pipe-bash, manual inspection instructions only | test_workspace_mirror.py::test_need_dg_returns_none_when_absent | N |
| `cdx_safe_remove_tree` | Y | workspace_mirror.py::safe_remove_tree — containment guard preserved as-is | test_workspace_mirror.py::test_safe_remove_tree_refuses_outside_root | N |
| `cdx_resolve_dg_root` | Y | workspace_mirror.py::resolve_dg_root | test_workspace_mirror.py::test_resolve_dg_root_refuses_home_dir | N |
| `cdx_dg_preflight` | N | folded into launch.py-style prepare/execute split when `dg` CLI integration is prioritized (not needed for the mirror-building logic itself) | — | N |
| `cdx_run_dg` | Y | workspace_mirror.py::run_dg | — | N |
| `cdx_run_dg_workspace` | Y | workspace_mirror.py::build_mirror (mirror-build) + run_dg_workspace (mirror-build + dg invocation) | test_workspace_mirror.py::test_build_mirror_creates_symlinks_and_index, test_build_mirror_deduplicates_colliding_basenames | N |
| `cdx_init_dg_workspace` | Y | workspace_mirror.py::init_workspace | test_workspace_mirror.py::test_init_workspace_creates_manifest, test_init_workspace_refuses_overwrite_without_force | N |
| `cdx_list_dg_workspaces` | Y | workspace_mirror.py::list_workspaces | test_workspace_mirror.py::test_list_workspaces | N |
| `cdx_show_dg_workspace` | Y | workspace_mirror.py::show_workspace | test_workspace_mirror.py::test_show_workspace_reports_presence | N |
| `cdx_clean_dg_workspace` | Y | workspace_mirror.py::clean_workspace | test_workspace_mirror.py::test_clean_workspace_removes_mirror | N |
| *(new)* `init_user` | Y | onboarding.py::init_user — **Workstream C completion**: `cdx-agent init-user` onboarding flow (config write, directory skeleton, packaged-default seeding, `--from-existing-user`-style adoption of shareable non-secret assets) | tests/test_onboarding.py (9 tests) | n/a |

## Step 8 — token/output helpers → mostly delete-duplication in favor of existing `token_tools.py`

Can proceed opportunistically in parallel with earlier steps — these mostly already have
Python equivalents.

| Bash function | Ported | Target module | Parity test | Bash delegates |
|---|---|---|---|---|
| `token_tool_script` | N | (delete — bash-only path resolver, no longer needed) | — | N |
| `token_rules_present_in_file` | N | doctor.py | — | N |
| `print_token_doctor` | N | doctor.py | — | N |
| `tool_install_command` | N | installer.py — **fix A5**: no unpinned `curl\|sh` for any tool | test_installer.py::test_no_installer_uses_unpinned_pipe_to_shell | N |
| `install_one_token_tool` | N | installer.py | — | N |
| `install_token_tools` | N | installer.py | — | N |
| `should_use_rtk_for_command` | N | token_tools.py (already has most of this logic) | — | N |
| `run_with_rtk_if_available` | N | token_tools.py | — | N |
| `run_and_capture_raw` | N | token_tools.py | — | N |
| `small_targeted_read_should_stay_raw` | N | token_tools.py | — | N |
| `summarize_raw_output` | Y (equiv exists) | token_tools.py — `command_summarize_output` already implements this | — | N |
| `run_usage_grouped_fallback` | N | token_tools.py (new: usage.py candidate if this grows) | — | N |
| `run_usage_session_fallback` | N | token_tools.py | — | N |
| `run_usage_daily` | N | token_tools.py | — | N |
| `run_usage_month` | N | token_tools.py | — | N |
| `run_usage_session` | N | token_tools.py | — | N |
| `run_context_budget` | Y | token_tools.py — `command_context_budget` already implements this | — | Y (bash already calls into a token_tools script) |
| `run_summarize_log` | Y | token_tools.py — `command_summarize_log` already implements this | — | Y |
| `run_compress_output` | Y (equiv exists) | token_tools.py — `command_summarize_output` covers this | — | N |

## Summary

- **Total bash functions inventoried**: 97
- **Ported with test coverage (post Phase 6)**: 58 — adds Step 7's dg-workspace mirroring
  in `workspace_mirror.py` (containment guard preserved, home-dir guard reused from
  `config.py`) and the new `onboarding.py::init_user` (Workstream C completion: config
  write, directory skeleton, packaged-default seeding, existing-tools-root adoption with
  a real-content-vs-empty-skeleton-dir distinction). Test suite: 140 tests across 11
  modules, all passing, ruff-clean. Two more real bugs were caught and fixed this pass:
  (1) `workspace_mirror`/`onboarding` tests exposed that only `graph.py`'s home-dir guard
  was being test-isolated, not `config.py`'s (which every other module's home-guard now
  routes through) — fixed by making `tests/conftest.py`'s isolation fixture patch
  `pathlib.Path.home` globally, not just `graph.HOME_ROOT`; (2) `onboarding.py`'s adoption
  logic originally skipped copying into any directory that merely *existed*, but
  `Config.directory_skeleton()` pre-creates every adoptable subdir empty before adoption
  runs, so adoption was silently never copying anything — fixed to check for actual
  directory content, not mere existence.
- **Bash delegates**: 6 command families — `launch` (the default, highest-traffic path),
  `session-doctor`, `install-hooks`, `validate-skills`, `skills-list`, and `skills-audit`
  now route through `cdx_agent_py()` (`python3 -m cdx_agent ...`), cut over on 2026-07-02
  with user authorization — see "Bash integration status" below for the full account.
  Everything else in the tables still runs bash's own implementation even where a Python
  port exists (e.g. the dg-workspace family and token/usage helpers are fully ported with
  parity tests but not yet delegated) — delegating those is backlog, not blocked.
- Steps are sequenced 1→8 above; see the plan's Phase 0–7 breakdown for how these map to
  shippable phases (each phase covers one or two steps plus its paired safety-hardening
  item).

## Bash integration status (as of Phase 7)

The Python CLI surface is now complete: `cli.py` wires `launch`, `session-doctor`,
`cancel-active`, `reap-stale-runtimes`, `resync`, `install-hooks`, `sync-docs`,
`skills-list`/`skills-audit`/`validate-skills`, `init-user`, and the full `dg`/
`dg-workspace-*` family, in addition to the pre-existing graph/token-tools commands. All of
it is reachable via `python3 -m cdx_agent <command> ...` and has been exercised both by
`tests/test_cli_commands.py` (unit-style, 15 tests) and by hand against a real stub-binary
repo (launch, session-doctor, skills, hooks, sync-docs, reap-stale-runtimes, resync,
dg-workspace round trip, and the `dg`-missing-binary error path all verified working
end-to-end before touching bash).

**`launch` also gained a `--secondary` mode** not in the original plan: a second concurrent
window into a repo that already has a live session no longer has to choose between fighting
the exclusive lock or using `--cancel-active` (which kills the live session). `--secondary`
joins the already-provisioned runtime directly — skips the lock, skips re-syncing
skills/hooks/docs (so it never races a live session's mid-flight reads of those files) — and
was verified to run to completion alongside a live primary without ever touching its lock.

User authorized cutting `bin/cdx-agent` over on 2026-07-02 ("I will be using your claude but
through cdx-agent... go free on Phase 7"). The bash script now delegates `launch` (the
default, highest-traffic path), `session-doctor`, `install-hooks`, `validate-skills`,
`skills-list`, and `skills-audit` to `python3 -m cdx_agent ...`, following the same
`PYTHONPATH`-injection pattern the script already used for `repo_graph_agent` (no pip install
dependency, via a new `cdx_agent_py()` bash helper). `runtime_codex_home()`/
`current_codex_home()` were also updated to fold `$ENGINE`/`$ACCESS_MODE` into the path
(the A1 fix, now live for every bash mode that resolves a runtime dir, not just `launch`);
the pre-fix shared path is kept reachable as `legacy_shared_codex_home()` so
`--usage`/`--token-doctor` can still find old sessions. New `--claude`/`--codex`/`--secondary`
flags were added to bash's arg parser. A pre-cutover backup of the original script is kept
alongside it (`cdx-agent.pre_python_cutover.<timestamp>`, matching the script's own
`backup_path` convention) in case of rollback.

**Two real bugs were caught during the cutover verification itself** (by actually dry-running
against real repos on this box, not just synthetic tests):
1. Without an explicit user config, `Config.defaults()` collapses `user_root` and
   `account_home` to the same `$HOME` — but this box genuinely uses two separate roots
   (`/common/users/ss5772` for work storage, `/common/home/ss5772` as the actual quota-limited
   `$HOME`). Bash's `--doctor` and the delegated `launch`'s dry-run output disagreed on the
   runtime path until an explicit `~/.config/cdx-agent/config.yaml` was written (via
   `init-user --user-root /common/users/ss5772 --tools-root /common/users/ss5772/codex_tools`)
   matching bash's existing `USER_ROOT`/`ACCOUNT_HOME`/`TOOLS_ROOT` constants exactly.
2. `onboarding._with_overrides` only replaced the `user_root` field when `--user-root` was
   passed, without recomputing `runtime_root`/`workspace_manifest_root`/
   `workspace_mirror_root` — which `Config.defaults()` derives *from* `user_root` at
   construction time. The override was silently non-cascading; fixed, with a regression test
   (`test_init_user_custom_user_root_recomputes_derived_paths`) that would have caught it.

**Verified end-to-end against the real system** (not just tests): `--doctor` still works;
`launch --dry-run` and a real (non-dry-run) launch against a throwaway repo correctly
provisioned an isolated runtime dir with synced `config.toml`/`auth.json` (0600), linked all
9 real skills from `codex_tools/skills` + `~/.agents/skills`, and generated a `hooks.json`
wiring all 4 hook scripts (confirming the A6 fix is live, not just tested); dry-runs against
the user's real `real_Cartpole` and `DeepReach` repos resolved correctly and — critically —
created no runtime directories or touched no files (dry-run really is dry). All test
artifacts created during this verification were cleaned up afterward.

## Post-cutover fix: runtime doc used the wrong template (2026-07-02)

Found while answering "do I get token savings launching through cdx-agent": `context_docs.py`
conflated two genuinely different bash templates under one `load_canonical_template`/
`DEFAULT_TEMPLATE` name -- `prepare_launch`'s runtime-dir doc sync was writing the condensed
per-repo template (`templates/repo.AGENTS.md`, meant for the repo's own committed AGENTS.md
via `sync-docs`) into the runtime directory instead of the fuller working-rules template
(`base/AGENTS.md`, bash's `BASE_AGENTS_TEMPLATE`) that actually carries the `TOKEN_SAVER`
block. Net effect: the session doc the agent actually reads at launch was the wrong,
much-shorter document, and the token-saving instructions were never reachable through the
new launch path at all, regardless of a `--token-saver` flag (which also didn't exist yet).

Fixed by splitting `context_docs.py` into two clearly separate paths:
- `load_working_rules_template`/`render_working_rules`/`sync_runtime_docs` -- the runtime-dir
  doc, with the same `TOKEN_SAVER_START`/`END` strip-by-default toggle bash's
  `render_runtime_agents` has (off by default, matching bash's `TOKEN_SAVER=0` default; no
  hand-written-file protection, since this file is regenerated fresh every launch).
- `load_repo_template`/`render`/`sync_repo_docs` -- unchanged behavior, renamed for clarity,
  still used by `sync-docs`/`sync_docs_for_repo` for the repo's own AGENTS.md/CLAUDE.md.

Added `--token-saver`/`--no-token-saver` to `cdx-agent launch` (Python and bash both --
bash's existing `$TOKEN_SAVER` variable, set by its own pre-existing `--token-saver` flag,
is now actually forwarded to the Python delegate instead of being a dead variable post-cutover).

Verified end-to-end against the real system: default launch correctly loads the real
`codex_tools/base/AGENTS.md` ("# Sandeep's Codex working rules...") with the token-saving
block stripped; `--token-saver` includes it. 10 new tests (`test_context_docs.py`,
`test_launch.py`, `test_cli_commands.py`).
