"""poll_guard: foreground wait loops and self-matching pgrep/pkill -f (table-driven)."""
import importlib.util
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[1] / "tools" / "hooks"
spec = importlib.util.spec_from_file_location("pre_tool_use_policy_pg", HOOKS / "pre_tool_use_policy.py")
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)

MULTI = "until ! pgrep -f '[p]ytest'\ndo\n  echo waiting\n  sleep 5\ndone\necho finished"
CASES = [
    ("multi-line until loop", MULTI, False, True),
    ("single-line while squeue", "while squeue -j 1 -h | grep -q .; do sleep 60; done", False, True),
    ("multi-line while test", "while [ ! -f /tmp/x ]\ndo sleep 2\ndone", False, True),
    ("while kill -0", "while kill -0 123; do sleep 1; done", False, True),
    ("bounded for retry", "for i in 1 2 3; do curl -sf http://x && break; sleep 1; done", False, False),
    ("while read without sleep", "while read line; do echo $line; done < file.txt", False, False),
    ("background run", MULTI, True, False),
    ("heredoc body", "cat > w.sh <<'EOF'\nuntil test -f x; do sleep 5; done\nEOF\necho ok", False, False),
    ("timeout 60 bounded", "timeout 60 bash -c 'until test -f /tmp/x; do sleep 1; done'", False, False),
    ("timeout 600 not bounded", "timeout 600 bash -c 'until test -f /tmp/x; do sleep 1; done'", False, True),
    ("pkill -f self-match", 'pkill -f "serve_tower"', False, True),
    ("pgrep -f self-match", "pgrep -f myjob", False, True),
    ("pgrep -af bracket trick", 'pgrep -af "[p]ytest"', False, False),
    ("pkill bracket trick", "pkill -f '[s]erve_tower'", False, False),
    ("pgrep by name", "pgrep python", False, False),
]


@pytest.mark.parametrize("name,cmd,background,denied", CASES, ids=[c[0] for c in CASES])
def test_poll_guard_table(name, cmd, background, denied):
    assert (policy.poll_guard(cmd, background) is not None) == denied


def test_pkill_text_in_commit_message_or_heredoc_allowed():
    assert policy.poll_guard('git commit -m "use pkill -f pytest to stop it"', False) is None
    assert policy.poll_guard("cat > n.md <<'X'\npkill -f pytest\nX\n", False) is None
    assert policy.poll_guard('pkill -f "pkill"', False) is not None
