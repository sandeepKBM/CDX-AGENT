import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"


def test_hook_modules_import_without_cli_or_graph():
    """Hooks import these under the system python (no PyYAML/tomli): they must not pull in cli/graph."""
    code = ("import sys; sys.path.insert(0, %r); "
            "from cdx_agent import delete_policy, inuse, trash, audit, values; "
            "bad = [m for m in ('cdx_agent.cli', 'cdx_agent.graph', 'yaml') if m in sys.modules]; "
            "print(bad); raise SystemExit(1 if bad else 0)") % str(SRC)
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr
