#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv"

find_python() {
  local candidate ok
  for candidate in python3.13 python3.12 python3.11 python3.10 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      ok="$("$candidate" - <<'PY'
import sys
print(int(sys.version_info >= (3, 10)))
PY
)"
      if [[ "$ok" == "1" ]]; then
        printf '%s\n' "$candidate"
        return 0
      fi
    fi
  done
  return 1
}

PYTHON="$(find_python || true)"
if [[ -z "${PYTHON:-}" ]]; then
  echo "Python 3.10+ is required but was not found on PATH." >&2
  exit 1
fi

echo "[install] Using Python: $("$PYTHON" --version)"

if [[ ! -x "$VENV/bin/python" ]]; then
  echo "[install] Creating virtual environment at $VENV"
  "$PYTHON" -m venv "$VENV"
fi

VENV_PYTHON="$VENV/bin/python"
VENV_CDX_AGENT="$VENV/bin/cdx-agent"

echo "[install] Upgrading pip"
"$VENV_PYTHON" -m pip install --upgrade pip

echo "[install] Installing CDX-AGENT in editable mode"
"$VENV_PYTHON" -m pip install -e "$ROOT"

echo "[install] Verifying cdx-agent --help"
"$VENV_CDX_AGENT" --help >/dev/null

echo "[install] Success"
echo "[install] To activate the environment:"
echo "  source \"$VENV/bin/activate\""
