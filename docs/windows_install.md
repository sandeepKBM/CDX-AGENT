# Windows Install

This repository is designed to install cleanly from a normal Windows user terminal.

## Requirements

- Python 3.10 or newer
- Git for Windows
- PowerShell

## One-step-ish install

```powershell
git clone https://github.com/sandeepKBM/CDX-AGENT.git
cd CDX-AGENT
powershell -ExecutionPolicy Bypass -File scripts/install.ps1
```

The installer will:
- check that Git is available
- check for a compatible Python interpreter
- create `.venv`
- upgrade `pip`
- install the package in editable mode
- verify `cdx-agent --help`

## Manual fallback

If you prefer to install manually:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -e .
.venv\Scripts\cdx-agent.exe --help
```

## Troubleshooting

- If the installer cannot find Python, install it from python.org or the Microsoft Store Python launcher package.
- If `cdx-agent --help` fails after install, remove `.venv` and run the installer again.
- The project does not depend on Conda or Rutgers HPC-specific paths.
