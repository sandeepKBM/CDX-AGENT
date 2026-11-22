"""NFS helpers."""

from __future__ import annotations

import os
import time
from collections.abc import Callable


def wait_visible(
    path: str | os.PathLike[str],
    timeout_s: float,
    poll_s: float = 1.0,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Wait until ``path`` is visible on this NFS client.

    Each attempt lists the parent directory (forces directory revalidation) and then stats the path.
    Always makes at least one attempt; ``timeout_s=0`` means a single attempt.
    """
    p = os.fspath(path)
    parent = os.path.dirname(os.path.abspath(p))
    deadline = clock() + timeout_s
    while True:
        try:
            os.listdir(parent)
        except OSError:
            pass
        try:
            os.stat(p)
            return True
        except OSError:
            pass
        if clock() >= deadline:
            return False
        sleep(poll_s)
