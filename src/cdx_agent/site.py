"""Site facts (user, roots, hosts, lock dir, trash roots, quota paths) read from one JSON file.

Lookup order: env ``CDX_SITE_JSON``, ``/common/users/ss5772/codex_tools/base/site.json``, then the
template shipped in ``tools/templates/site.json``.  Modules call ``site.get("hosts.gateway")``-style
dotted keys instead of hard-coding names::

    from cdx_agent import site
    site.get("hosts.gateway", default="rlab1")
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

LIVE_PATH = "/common/users/ss5772/codex_tools/base/site.json"
TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "tools" / "templates" / "site.json"
_MISSING = object()


def site_path() -> Optional[Path]:
    env = os.environ.get("CDX_SITE_JSON")
    candidates = [Path(env)] if env else []
    candidates += [Path(LIVE_PATH), TEMPLATE_PATH]
    for path in candidates:
        if path.is_file():
            return path
    return None


def load() -> Dict[str, Any]:
    path = site_path()
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def get(dotted_key: str, default: Any = None) -> Any:
    """Value at ``a.b.c`` in the site file, ``default`` when any part is missing."""
    node: Any = load()
    for part in dotted_key.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node
