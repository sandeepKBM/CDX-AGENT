"""AGENTS.md / CLAUDE.md generation from one canonical source.

Implements **D2**: both files are generated outputs of the same render
function over one canonical rules template (the existing
`codex_tools/templates/repo.AGENTS.md`), differing only by filename -- the
template itself is already engine-agnostic. `sync_repo_docs` never silently
overwrites a pre-existing, hand-written file (e.g. a real project's
hand-authored CLAUDE.md, such as `DeepReach/deepreach/CLAUDE.md`): a
generated-file marker comment must already be present before this module
will touch a target again. If it's absent, the sync refuses and the caller
must explicitly pass ``adopt=True`` (append canonical rules after the
existing content) or ``force=True`` (overwrite, with a backup taken first).

This is intentionally *stricter* than the bash predecessor's
`write_repo_agents`, which auto-appended to any existing AGENTS.md lacking
the marker without asking -- safe enough for AGENTS.md (Codex has always
gone through this path), but not a policy this module extends to CLAUDE.md,
since that file may already carry real, hand-written project documentation
that predates any cdx_agent involvement.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .config import Config, backup_path

Engine = Literal["codex", "claude"]

GENERATED_MARKER = "<!-- cdx-agent managed guidance -->"
FILENAME_BY_ENGINE: dict[Engine, str] = {"codex": "AGENTS.md", "claude": "CLAUDE.md"}

DEFAULT_TEMPLATE = f"""
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


def load_canonical_template(config: Config) -> str:
    template_path = config.tools_root / "templates" / "repo.AGENTS.md"
    if template_path.is_file():
        return template_path.read_text()
    return DEFAULT_TEMPLATE


def render(template_text: str, repo_name: str) -> str:
    """Render the canonical rules template for a specific repo. The template
    is engine-agnostic today, so `engine` only affects the output filename
    (see `target_path`), not the content -- if per-engine wording is ever
    needed, this is the single seam to add it."""
    rendered = template_text.replace("__REPO_NAME__", repo_name)
    if GENERATED_MARKER not in rendered:
        rendered = f"{GENERATED_MARKER}\n\n{rendered}"
    return rendered


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


__all__ = [
    "DEFAULT_TEMPLATE",
    "Engine",
    "FILENAME_BY_ENGINE",
    "GENERATED_MARKER",
    "SyncResult",
    "is_generated",
    "load_canonical_template",
    "render",
    "sync_repo_docs",
    "target_path",
]
