import importlib.util
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[1] / "tools" / "hooks"
sys.path.insert(0, str(HOOKS))
spec = importlib.util.spec_from_file_location("slurm_guard", HOOKS / "slurm_guard.py")
sg = importlib.util.module_from_spec(spec)
sys.modules["slurm_guard"] = sg
spec.loader.exec_module(sg)


@pytest.mark.parametrize("cmd", [
    "sbatch job.sbatch",
    "cd x && srun -p gpu --gres=gpu:1 python t.py",
    "ssh rlab1 sbatch /x/j.sbatch",
    "ssh -o BatchMode=yes rlab1 \"cd /x && sbatch j.sbatch\"",
    "bash -c 'srun --jobid=1 --overlap hostname'",
    "timeout 60 salloc -N1",
])
def test_raw_launch_denied(cmd, monkeypatch):
    monkeypatch.delenv("CDX_RAW_SLURM", raising=False)
    reason = sg.check(cmd)
    assert reason and "cdx-agent --run" in reason


@pytest.mark.parametrize("cmd", [
    "squeue -u ss5772",
    "sacct -j 123 --format=State",
    "ssh rlab1 squeue -u ss5772",
    "grep -rn sbatch slurm/",
    "echo 'use sbatch here'",
    "CDX_RAW_SLURM=1 sbatch odd.sbatch",
    "cdx-agent --run --cpus 4 -- python x.py",
])
def test_allowed(cmd, monkeypatch):
    monkeypatch.delenv("CDX_RAW_SLURM", raising=False)
    assert sg.check(cmd) is None
