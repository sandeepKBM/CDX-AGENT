# Linux / HPC Install

This project installs cleanly in a local virtual environment and does not require Conda.

## Quick install

```bash
git clone https://github.com/sandeepKBM/CDX-AGENT.git
cd CDX-AGENT
bash scripts/install.sh
```

The installer will:
- check for Python 3.10+
- create `.venv`
- upgrade `pip`
- install the package in editable mode
- verify `cdx-agent --help`

## Manual install

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e .
.venv/bin/cdx-agent --help
```

## HPC notes

- Do not point the project at machine-specific paths.
- Keep logs, checkpoints, datasets, and `.codex_graph/` out of version control.
- If you are working inside a large monorepo, run `cdx-agent --graph` from the intended project root, not from your home directory.
