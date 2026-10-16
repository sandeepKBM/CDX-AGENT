"""One-shot notifier over a JSONL event log written by some other persistent logger.

    python -m cdx_agent.notify EVENTS.jsonl [--routine REGEX_FILE] [--settle S] [--stale S]
        [--done-kinds END,EMPTY] [--alive FILE] [--then "CMD"] [--poll S] [--max-wait S]

Starts at the current end of EVENTS and exits (printing why) on the first of:
  ANOMALY      an event whose "KIND text" line matches none of the routine regexes
               (re.match, i.e. anchored at the start; done-kind events are never anomalies);
  DONE         a done-kind event was seen and SETTLE seconds passed since the first one;
  LOGGER DOWN  the heartbeat file (default EVENTS + ".alive") is missing or older than STALE s
               (--stale 0 disables the check);
  TIMEOUT      --max-wait seconds passed (default 0 = wait forever).
Then it runs --then CMD (bash -c) and prints its output. It only ever opens EVENTS read-only.

Routine regex file: one regex per line; blank lines and lines starting with '#' are ignored.
Exit codes: 0 DONE, 2 ANOMALY, 3 LOGGER DOWN, 4 TIMEOUT, 1 usage error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from typing import Callable, List, Optional, Pattern, Tuple

EXIT_DONE, EXIT_ANOMALY, EXIT_STALE, EXIT_TIMEOUT = 0, 2, 3, 4


def load_routine(path: Optional[str]) -> List[Pattern[str]]:
    """One regex per line, '#' comments and blank lines skipped. Bad regexes raise ValueError."""
    if not path:
        return []
    out: List[Pattern[str]] = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.rstrip("\n").rstrip("\r")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            try:
                out.append(re.compile(line))
            except re.error as exc:
                raise ValueError(f"{path}:{lineno}: bad regex {line!r}: {exc}") from exc
    return out


def event_line(event: dict) -> str:
    return f"{event.get('kind')} {event.get('text', '')}"


def is_routine(line: str, routine: List[Pattern[str]]) -> bool:
    return any(p.match(line) for p in routine)


class Tail:
    """Reads complete new lines appended to a file, starting at its end when created."""

    def __init__(self, path: str, from_start: bool = False) -> None:
        self.path = path
        self.offset = 0 if from_start else os.path.getsize(path)
        self.truncations = 0

    def read_lines(self) -> List[str]:
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return []
        if size < self.offset:  # truncated / replaced by the logger: start over
            self.offset = 0
            self.truncations += 1
        with open(self.path, "rb") as fh:  # read-only, never written
            fh.seek(self.offset)
            chunk = fh.read()
        end = chunk.rfind(b"\n")
        if end < 0:
            return []  # only a partial line so far: wait for its newline
        self.offset += end + 1
        return chunk[: end + 1].decode("utf-8", errors="replace").splitlines()


def heartbeat_age(alive: str, now: float) -> Optional[float]:
    try:
        return now - os.path.getmtime(alive)
    except OSError:
        return None


def watch(
    events: str,
    routine: List[Pattern[str]],
    done_kinds: Tuple[str, ...] = ("END", "EMPTY"),
    settle: float = 90.0,
    stale: float = 300.0,
    alive: Optional[str] = None,
    poll: float = 20.0,
    max_wait: float = 0.0,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> Tuple[int, str]:
    """Blocks until a stop condition; returns (exit_code, reason)."""
    alive = alive or events + ".alive"
    tail = Tail(events)
    start = clock()
    first_done: Optional[float] = None
    n_done = 0
    n_seen = 0
    while True:
        for line in tail.read_lines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            n_seen += 1
            if str(event.get("kind")) in done_kinds:
                n_done += 1
                if first_done is None:
                    first_done = clock()
                continue
            text = event_line(event)
            if not is_routine(text, routine):
                stamp = event.get("t")
                return EXIT_ANOMALY, " ".join(["ANOMALY"] + ([str(stamp)] if stamp else []) + [text[:220]])
        now = clock()
        if stale > 0:
            age = heartbeat_age(alive, now)
            if age is None:
                return EXIT_STALE, f"LOGGER DOWN: heartbeat {alive} missing"
            if age > stale:
                return EXIT_STALE, f"LOGGER DOWN: heartbeat {alive} stale ({age:.0f} s > {stale:.0f} s)"
        if first_done is not None and now - first_done >= settle:
            return EXIT_DONE, (
                f"DONE: {n_done} done-kind event(s) ({','.join(done_kinds)}), settled {now - first_done:.0f} s;"
                f" {n_seen} event(s) read"
            )
        if max_wait > 0 and now - start >= max_wait:
            return EXIT_TIMEOUT, f"TIMEOUT: no stop condition within {max_wait:.0f} s; {n_seen} event(s) read"
        sleep(poll)


def run_then(cmd: str, timeout: float = 300.0) -> int:
    try:
        r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"then: timed out after {timeout:.0f} s: {cmd}")
        return 124
    out = r.stdout.strip()
    print(out if out else r.stderr.strip()[-2000:])
    print(f"then_rc={r.returncode}")
    return r.returncode


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cdx-agent --notify",
        description="One-shot notifier over a JSONL event log (read-only). See module docstring.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("events", help="JSONL event log written by another (persistent) logger")
    p.add_argument("--routine", help="file with one regex per line (# comments); matched against 'KIND text'")
    p.add_argument("--settle", type=float, default=90.0, help="seconds after the first done-kind event (default 90)")
    p.add_argument("--stale", type=float, default=300.0, help="max heartbeat age in s, 0 disables (default 300)")
    p.add_argument("--done-kinds", default="END,EMPTY", help="comma-separated kinds that mean done (default END,EMPTY)")
    p.add_argument("--alive", help="heartbeat file (default EVENTS + '.alive')")
    p.add_argument("--then", dest="then_cmd", help="shell command to run after exiting; its output is printed")
    p.add_argument("--then-timeout", type=float, default=300.0, help="timeout for --then in s (default 300)")
    p.add_argument("--poll", type=float, default=20.0, help="poll interval in s (default 20)")
    p.add_argument("--max-wait", type=float, default=0.0, help="give up after S seconds (default 0 = never)")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not os.path.isfile(args.events):
        print(f"events file not found: {args.events}", file=sys.stderr)
        return 1
    try:
        routine = load_routine(args.routine)
    except (OSError, ValueError) as exc:
        print(f"routine file error: {exc}", file=sys.stderr)
        return 1
    done_kinds = tuple(k.strip() for k in args.done_kinds.split(",") if k.strip())
    try:
        rc, why = watch(
            args.events, routine, done_kinds=done_kinds, settle=args.settle, stale=args.stale,
            alive=args.alive, poll=args.poll, max_wait=args.max_wait,
        )
    except KeyboardInterrupt:
        print("interrupted")
        return 130
    print(why, flush=True)
    if args.then_cmd:
        run_then(args.then_cmd, timeout=args.then_timeout)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
