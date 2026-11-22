"""``python -m cdx_agent.site_facts``: live caps and free space in a few lines.  Never fails (exit 0)."""
from __future__ import annotations

import shutil
import subprocess
import sys
from typing import List

from cdx_agent import site


def _caps(user: str) -> str:
    try:
        from cdx_agent.gpu_pick import read_grptres

        gpus, nodes = read_grptres(user, site.get("hosts.gateway", "rlab1"), default=(-1, -1))
    except Exception as exc:  # noqa: BLE001 - this report must never fail
        return f"caps: unavailable ({str(exc)[:60] or type(exc).__name__})"
    if gpus < 0 and nodes < 0:
        return "caps: unavailable (sacctmgr unreadable)"
    parts = [f"gpus={gpus}" if gpus >= 0 else "gpus=unset", f"nodes={nodes}" if nodes >= 0 else "nodes=unset"]
    return "caps: GrpTRES " + " ".join(parts)


def _df(path: str) -> str:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return f"free {path}: unavailable"
    return f"free {path}: {usage.free / 2**30:.0f}G of {usage.total / 2**30:.0f}G"


def lines() -> List[str]:
    user = str(site.get("user", "ss5772"))
    out = [_caps(user)]
    seen: List[str] = []
    for path in [site.get("user_root"), "/data/local", "/freespace/local"]:
        if isinstance(path, str) and path not in seen:
            seen.append(path)
            out.append(_df(path))
    if shutil.which("quota"):
        try:
            res = subprocess.run(["quota", "-s"], capture_output=True, text=True, timeout=20)
            text = [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()]
            out.extend(f"quota: {ln}" for ln in text[:6])
        except (OSError, subprocess.SubprocessError):
            out.append("quota: unavailable")
    return out[:12]


def main(argv: List[str] | None = None) -> int:
    try:
        print("\n".join(lines()))
    except Exception as exc:  # noqa: BLE001
        print(f"site-facts: unavailable ({exc})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
