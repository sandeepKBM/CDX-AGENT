"""Run event log: <run_dir>/events.jsonl (+ ``.alive`` heartbeat) in the shape ``cdx-agent --notify`` reads.

Each line: {"event": START|UNIT|END, "kind": same, "t": epoch, "time": iso, "text": ..., ...fields}. notify.py keys on
``kind`` (done kinds END), ``t`` and ``text``; ``event`` and ``time`` are the spec names. END removes the heartbeat.
Heartbeat files: ``.alive`` in the run dir (spec) and ``events.jsonl.alive`` (notify's default).
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

EVENTS = "events.jsonl"


class RunLog:
    def __init__(self, run_dir: str) -> None:
        self.run_dir = run_dir
        self.path = os.path.join(run_dir, EVENTS)
        self.alive_paths = [os.path.join(run_dir, ".alive"), self.path + ".alive"]

    def _touch(self) -> None:
        for p in self.alive_paths:
            with open(p, "a"):
                pass
            os.utime(p, None)

    def _emit(self, event: str, text: str, **fields: Any) -> None:
        os.makedirs(self.run_dir, exist_ok=True)
        now = time.time()
        rec = {"event": event, "kind": event, "t": now,
               "time": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)), "text": text, **fields}
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def start(self, **meta: Any) -> None:
        self._emit("START", str(meta.get("name", "")), **meta)
        self._touch()

    def unit(self, id: str, **fields: Any) -> None:  # noqa: A002
        self._emit("UNIT", f"{id} {fields.get('state', '')}".strip(), id=id, **fields)
        self._touch()

    def end(self, state: str, **fields: Any) -> None:
        self._emit("END", state, state=state, **fields)
        for p in self.alive_paths:
            try:
                os.remove(p)
            except OSError:
                pass
