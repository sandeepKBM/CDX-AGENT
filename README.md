# CDX-AGENT

CDX-AGENT packages the local repository graph and context tooling into a clean, cross-platform Python project.

What it does:
- builds `.codex_graph/repo_graph.json` and `context_pack.md`
- detects workspace dependencies and cross-repo edges
- summarizes noisy logs and command output
- provides safer `find`, `rg`, and `git diff` helpers

## Install

For development:

```bash
pip install -e .
```

For a normal install:

```bash
pip install .
```

For contributor tooling:

```bash
pip install -e ".[dev]"
```

## Windows quick install

```powershell
git clone https://github.com/sandeepKBM/CDX-AGENT.git
cd CDX-AGENT
powershell -ExecutionPolicy Bypass -File scripts/install.ps1
```

## Linux / HPC quick install

```bash
git clone https://github.com/sandeepKBM/CDX-AGENT.git
cd CDX-AGENT
bash scripts/install.sh
```

## Quick start

Build graph data for the current repository:

```bash
cdx-agent --graph --repo .
```

Generate a context pack for a specific task:

```bash
cdx-agent --context --repo . --task "update the training launcher"
```

Rank relevant files:

```bash
cdx-agent --relevant --repo . --task "fix Windows install flow"
```

Summarize a log:

```bash
cdx-agent --summarize-log logs/run.log
```

Helpers (see `cdx-agent --help`, section Helpers):

```bash
cdx-agent --offload "<self-contained prompt>"            # Codex reasoning offload, pinned model, read-only, stdin closed
cdx-agent --notify events.jsonl --routine routine.txt --then "<summary cmd>"   # one-shot wake: 0 settled, 2 anomaly, 3 logger down, 4 timeout
cdx-agent --move-verify SRC DST                          # copy + checksum/size verify; prints the user's delete+symlink line, never deletes
cdx-agent --gpu-lock HOLD_JOBID --n 1 -- <cmd>           # run inside a shared Slurm GPU hold with flock'd GPUs; --gpu-lock --status lists holders
```
`--doctor` also checks the Codex install (binary link, CLI version, configured model available).

## Project layout

- `src/cdx_agent/` contains the packaged Python modules
- `scripts/` contains the install helpers
- `docs/` contains install and configuration notes
- `examples/config.example.yaml` shows the workspace config format

## Notes

- The generated `.codex_graph/` directory is ignored by Git.
- The CLI keeps the legacy `--graph` style for compatibility, but the canonical install target is the `cdx-agent` console script.
- `safe-rg` requires `rg` to be available on `PATH`.
