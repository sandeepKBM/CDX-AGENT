"""AGENTS.md / CLAUDE.md generation.

There are two distinct documents this module generates, matching the bash
predecessor's two separate templates (which cdx_agent's Phase 3/4 port had
originally conflated into one -- see the A6-adjacent fix below):

- **Working-rules doc** (`load_working_rules_template` /
  `render_working_rules` / `sync_runtime_docs`): the fuller "how to work in
  this environment" rules (bash's `BASE_AGENTS_TEMPLATE`,
  `codex_tools/base/AGENTS.md`), written into the per-launch **runtime**
  directory -- this is what the agent actually reads at session start. It
  includes an opt-in `TOKEN_SAVER` block (stripped by default, matching
  bash's `render_runtime_agents`/`TOKEN_SAVER=0` default) with instructions
  to prefer targeted reads/summaries over dumping full files/logs/diffs.
  Regenerated fresh on every launch; no hand-written-file protection, since
  nothing is expected to hand-edit a runtime-only, ephemeral file.
- **Repo doc** (`load_repo_template` / `render` / `sync_repo_docs`): the
  condensed per-repo template (bash's `REPO_AGENTS_TEMPLATE`,
  `codex_tools/templates/repo.AGENTS.md`), written into the **repo's own**
  `AGENTS.md`/`CLAUDE.md` (implements **D2**). `sync_repo_docs` never
  silently overwrites a pre-existing, hand-written file (e.g. a real
  project's hand-authored CLAUDE.md, such as `DeepReach/deepreach/CLAUDE.md`):
  a generated-file marker comment must already be present before this module
  will touch a target again. If it's absent, the sync refuses and the caller
  must explicitly pass ``adopt=True`` (append canonical rules after the
  existing content) or ``force=True`` (overwrite, with a backup taken first).
  This is intentionally *stricter* than bash's `write_repo_agents`, which
  auto-appended without asking.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .config import Config, backup_path

Engine = Literal["codex", "claude"]

GENERATED_MARKER = "<!-- cdx-agent managed guidance -->"
TOKEN_SAVER_START_MARKER = "<!-- TOKEN_SAVER_START -->"
TOKEN_SAVER_END_MARKER = "<!-- TOKEN_SAVER_END -->"
FILENAME_BY_ENGINE: dict[Engine, str] = {"codex": "AGENTS.md", "claude": "CLAUDE.md"}

DEFAULT_REPO_TEMPLATE = f"""
{GENERATED_MARKER}

Repo-specific guidance for `__REPO_NAME__`:

- Read `.codex_graph/context_pack.md` before non-trivial edits when it exists.
- If `.codex_graph/context_pack.md` is missing or stale, suggest `cdx-agent --graph`.
- When `.codex_graph/workspace_context_pack.md` exists, read it before editing.
- Treat dependency repos as read-only unless the task explicitly says to edit them.
- Identify the active launcher, config path, and call chain before editing policy, training, env, or controller files.
- State checkpoint, dataset, and log path assumptions before touching ML or robotics execution code.
- Prefer smoke tests and one-batch checks before long runs.
- Include rollback instructions in the final response.
"""

DEFAULT_WORKING_RULES_TEMPLATE = f"""# Codex/Claude working rules

Before non-trivial edits:
1. Identify the active entrypoint.
2. Identify the config/YAML path.
3. Identify the policy/controller/env/training/eval call chain.
4. Identify checkpoint, dataset, and log paths.
5. Read `.codex_graph/context_pack.md` if present.
6. If graph context is stale or missing, suggest running `cdx-agent --graph`.
7. Give impact analysis before editing training/eval/controller/policy files.
8. Start with `docs/` and `docs/runbooks/` for baseline understanding; they usually capture the latest useful variant of the codebase, but they are not perfect.
9. If docs drift from live behavior, notify the user and ask before making the smallest docs-only fix.
10. Prefer trimming redundant, stale, or generated documentation, and keep experimentation notes in `docs/status/*` or `reports/` instead of scattering new prose.

# Multi-repo workspace rules

When `.codex_graph/workspace_context_pack.md` exists:
1. Read it before editing.
2. Treat the primary repo as the default edit scope.
3. Treat dependency repos as read-only unless the task explicitly says to edit them.
4. Trace bugs across dependency repos, but provide impact analysis before cross-repo edits.
5. If dependency code looks implicated, first propose a config fix, wrapper fix, adapter fix, or version/path fix before modifying the dependency repo itself.
6. Never silently edit dependency/vendored/third-party repos.

Never edit without explicit request:
- checkpoints
- datasets
- logs
- `.git`
- generated experiment artifacts
- old result folders
- large binary/model files

For robotics/ML repos:
- Prefer tiny smoke tests before long training.
- Do not change training and eval logic together unless requested.
- Do not silently change units, control frequency, action scaling, horizon length, or checkpoint selection.
- When changing controllers, state the control-rate and action-scaling assumptions.
- When changing data generation, state what distribution shift is introduced.
- When changing configs, preserve the old config or create a new named config.

{TOKEN_SAVER_START_MARKER}
# Token-saving rules

Token-saving mode may be active.

Before exploring:
1. Read `.codex_graph/context_pack.md` if present.
2. Prefer targeted searches over broad scans.
3. Use `rg` with narrow patterns and path filters.
4. Use `sed -n` with small line ranges.
5. Use `git diff --stat` before full `git diff`.
6. Use `cdx-agent --context-budget` when repo looks huge.
7. Use `cdx-agent --summarize-log <file>` for large logs.
8. Use `cdx-agent --compress-output <command>` for noisy commands.

Avoid:
- `cat` on huge files
- full recursive `find` output
- full recursive grep into generated artifacts
- dumping full logs
- dumping full diffs
- reading checkpoints/datasets/logs/generated folders unless requested

Never hide:
- errors
- tracebacks
- failed assertions
- changed-file diffs
- exact commands run
- exit codes
- checkpoint/config/data paths relevant to the task

For ML/robotics:
- Do not read full wandb logs or videos.
- Summarize train/eval logs first.
- Preserve final metrics, failure lines, config paths, checkpoint paths.
- When diagnosing controller/eval issues, use targeted grep for control frequency, action scaling, horizon, checkpoint, env reset, and policy wrapper.
{TOKEN_SAVER_END_MARKER}

For every final response:
- list files changed
- list tests run
- list tests not run
- list rollback command
"""


def load_repo_template(config: Config) -> str:
    """The condensed per-repo template, for the repo's own AGENTS.md/CLAUDE.md."""
    template_path = config.tools_root / "templates" / "repo.AGENTS.md"
    if template_path.is_file():
        return template_path.read_text()
    return DEFAULT_REPO_TEMPLATE


def load_working_rules_template(config: Config) -> str:
    """The fuller working-rules template, for the runtime dir's AGENTS.md/CLAUDE.md."""
    template_path = config.tools_root / "base" / "AGENTS.md"
    if template_path.is_file():
        return template_path.read_text()
    return DEFAULT_WORKING_RULES_TEMPLATE


def render(template_text: str, repo_name: str) -> str:
    """Render the repo-doc template for a specific repo. The template is
    engine-agnostic today, so `engine` only affects the output filename (see
    `target_path`), not the content -- if per-engine wording is ever needed,
    this is the single seam to add it."""
    rendered = template_text.replace("__REPO_NAME__", repo_name)
    if GENERATED_MARKER not in rendered:
        rendered = f"{GENERATED_MARKER}\n\n{rendered}"
    return rendered


def render_working_rules(template_text: str, token_saver: bool = False) -> str:
    """Render the working-rules template, matching bash's
    `render_runtime_agents`: the block between TOKEN_SAVER_START/END markers
    is stripped unless `token_saver=True` (default off, matching bash's
    `TOKEN_SAVER=0` default -- token-saving mode is opt-in via
    `--token-saver`, not on by default)."""
    if token_saver:
        return template_text
    lines = template_text.splitlines(keepends=True)
    out: list[str] = []
    skip = False
    for line in lines:
        if TOKEN_SAVER_START_MARKER in line:
            skip = True
            continue
        if TOKEN_SAVER_END_MARKER in line:
            skip = False
            continue
        if not skip:
            out.append(line)
    return "".join(out)


def target_path(repo: Path, engine: Engine) -> Path:
    return repo / FILENAME_BY_ENGINE[engine]


def is_generated(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        content = path.read_text(errors="ignore")
    except OSError:
        return False
    return GENERATED_MARKER in content


@dataclass(frozen=True)
class SyncResult:
    path: Path
    action: Literal["created", "updated", "unchanged", "refused_hand_written", "adopted"]
    detail: str = ""


def sync_repo_docs(
    repo: Path,
    template_text: str,
    engine: Engine = "codex",
    adopt: bool = False,
    force: bool = False,
) -> SyncResult:
    path = target_path(repo, engine)
    rendered = render(template_text, repo.name)

    if not path.exists():
        path.write_text(rendered)
        return SyncResult(path, "created")

    if is_generated(path):
        if path.read_text(errors="ignore") == rendered:
            return SyncResult(path, "unchanged")
        backup_path(path)
        path.write_text(rendered)
        return SyncResult(path, "updated")

    if force:
        backup_path(path)
        path.write_text(rendered)
        return SyncResult(path, "updated", detail="force-overwrote a hand-written file")

    if adopt:
        existing = path.read_text(errors="ignore")
        backup_path(path)
        path.write_text(f"{existing.rstrip()}\n\n{rendered}")
        return SyncResult(path, "adopted", detail="appended canonical rules after existing hand-written content")

    return SyncResult(
        path,
        "refused_hand_written",
        detail=f"{path} exists and has no cdx-agent generated marker; pass adopt=True or force=True to proceed",
    )


def sync_runtime_docs(runtime_dir: Path, content: str, engine: Engine = "codex") -> SyncResult:
    """Write the working-rules doc into a per-launch runtime dir. No
    hand-written-file protection here (unlike `sync_repo_docs`) -- this file
    is regenerated fresh on every launch and nothing is expected to hand-edit
    it, matching bash's `ensure_runtime_agents` (content-diff, backup if
    changed, overwrite)."""
    path = target_path(runtime_dir, engine)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return SyncResult(path, "created")
    if path.read_text(errors="ignore") == content:
        return SyncResult(path, "unchanged")
    backup_path(path)
    path.write_text(content)
    return SyncResult(path, "updated")


__all__ = [
    "DEFAULT_REPO_TEMPLATE",
    "DEFAULT_WORKING_RULES_TEMPLATE",
    "Engine",
    "FILENAME_BY_ENGINE",
    "GENERATED_MARKER",
    "TOKEN_SAVER_END_MARKER",
    "TOKEN_SAVER_START_MARKER",
    "SyncResult",
    "is_generated",
    "load_repo_template",
    "load_working_rules_template",
    "render",
    "render_working_rules",
    "sync_repo_docs",
    "sync_runtime_docs",
    "target_path",
]
