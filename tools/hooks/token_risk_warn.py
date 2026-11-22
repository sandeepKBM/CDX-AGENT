#!/usr/bin/env python3
"""No-op: token-risk warnings are emitted by pre_tool_use_policy.py (this script used to duplicate them).

Kept as an installed file name so existing hook configs keep working. Always exits 0 silently.
"""
from __future__ import annotations

import sys


def main() -> int:
    try:
        sys.stdin.read()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:
        raise SystemExit(0)
