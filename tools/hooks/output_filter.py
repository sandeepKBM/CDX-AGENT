#!/usr/bin/env python3
"""Retired PreToolUse hook (Bash). Long-output capping moved to output_cap.py (PostToolUse).

Kept so settings of already-running sessions that still call this script keep working: it makes no permission decision,
rewrites nothing and prints nothing for any payload.
"""
from __future__ import annotations


def main() -> int:
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
