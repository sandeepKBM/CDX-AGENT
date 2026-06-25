# Configuration

CDX-AGENT has two common configuration surfaces:

## 1. Repository graph output

Running `cdx-agent --graph` or `cdx-agent graph` writes:

- `.codex_graph/repo_graph.json`
- `.codex_graph/entrypoints.json`
- `.codex_graph/config_edges.json`
- `.codex_graph/context_pack.md`

These files are generated artifacts and should stay untracked.

## 2. Workspace configuration

`cdx-agent --init-workspace` writes `.codex_graph/workspace.yaml`.

Example structure:

```yaml
primary_repo: /path/to/primary/repo
dependency_repos:
  - name: openpi
    package: openpi
    repo_root: /path/to/OpenPI
    mode: read_only
    editable: false
    present: true
exclude_dirs:
  - .git
  - .venv
  - checkpoints
  - data
  - logs
include_third_party_dependencies: false
edit_policy:
  primary_repo: editable
  dependency_repos: read_only_unless_explicit
scan_limits:
  primary: 20000
  dependency: 5000
```

## Environment variables

- `CDX_AGENT_INCLUDE_THIRD_PARTY_DEPS`
  - Set to `1`, `true`, `yes`, or `on` to include third-party dependency roots in workspace scans.

## Practical guidance

- Keep editable work limited to the primary repository.
- Treat dependency repositories as read-only unless the task explicitly says otherwise.
- Avoid scanning your home directory unless you really mean to.
