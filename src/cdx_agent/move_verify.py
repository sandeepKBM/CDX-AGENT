"""Copy a directory tree to a new place, prove the copy is complete, and hand the USER the delete+symlink line.

    python -m cdx_agent.move_verify SRC DST [--bwlimit KBPS] [--no-lsof] [--verify-only]

1. Refuses if SRC is a symlink or not a directory, if DST is inside SRC (or SRC inside DST), if SRC is a
   top-level/home-like directory, or if a process on THIS host has a file (or its cwd) under SRC open
   (an lsof-style /proc scan; --no-lsof skips it; processes on other hosts are invisible to it).
2. ionice -c3 nice -n 19 rsync -a [--bwlimit=KBPS] SRC/ DST/      (skipped with --verify-only)
3. Verifies with `rsync -a --checksum --dry-run --itemize-changes SRC/ DST/` (must list nothing) AND equal
   file-only byte totals plus file / symlink / directory / other counts (st_size of regular files only;
   `du --apparent-size` is NOT used: directory sizes differ across NFS/ext4).
4. On success prints VERIFIED. With --finish it then moves SRC to the trash (`cdx-agent --rm --verified-copy`:
   a protected source becomes tier b after a verified copy, decision Q1 2026-11-22; restorable for trash.ttl_days)
   and creates the symlink SRC -> DST. Without --finish nothing is deleted and the --finish line is printed.
Exit codes: 0 verified, 1 copy/verify failure, 2 refused.
"""
from __future__ import annotations

import argparse
import os
import shlex
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

EXIT_OK, EXIT_FAIL, EXIT_REFUSED = 0, 1, 2


@dataclass(frozen=True)
class TreeStats:
    files: int = 0
    bytes: int = 0
    symlinks: int = 0
    dirs: int = 0
    other: int = 0


def tree_stats(root: str) -> TreeStats:
    """Counts below root (root itself excluded); never follows symlinks. bytes = sum of regular-file st_size."""
    files = nbytes = links = dirs = other = 0
    stack = [root]
    while stack:
        d = stack.pop()
        with os.scandir(d) as it:
            for entry in it:
                st = entry.stat(follow_symlinks=False)
                mode = st.st_mode
                if stat.S_ISLNK(mode):
                    links += 1
                elif stat.S_ISDIR(mode):
                    dirs += 1
                    stack.append(entry.path)
                elif stat.S_ISREG(mode):
                    files += 1
                    nbytes += st.st_size
                else:
                    other += 1
    return TreeStats(files, nbytes, links, dirs, other)


def human(n: int) -> str:
    x = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if x < 1024 or unit == "TiB":
            return f"{x:.0f} {unit}" if unit == "B" else f"{x:.2f} {unit}"
        x /= 1024
    return f"{n} B"


def _inside(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent.rstrip("/") + "/")


def check_paths(src: str, dst: str) -> Optional[str]:
    """Returns a refusal reason or None. src/dst are as given by the user."""
    s = src.rstrip("/") or "/"
    d = dst.rstrip("/") or "/"
    if os.path.islink(s):
        return f"SRC {s} is already a symlink (-> {os.readlink(s)}); nothing to move"
    if not os.path.isdir(s):
        return f"SRC {s} is not an existing directory"
    rs, rd = os.path.realpath(s), os.path.realpath(d)
    if rs == rd:
        return f"SRC and DST are the same directory ({rs})"
    if _inside(rd, rs):
        return f"DST {rd} is inside SRC {rs}"
    if _inside(rs, rd):
        return f"SRC {rs} is inside DST {rd}"
    home = os.path.realpath(os.path.expanduser("~"))
    user = os.environ.get("USER", "")
    if rs.count("/") <= 2 or _inside(home, rs) or (user and os.path.basename(rs) == user and rs.count("/") <= 3):
        return f"SRC {rs} is a top-level or home-like directory; refusing to stage its deletion"
    if os.path.exists(d) and not os.path.isdir(d):
        return f"DST {d} exists and is not a directory"
    return None


def open_under(root: str, proc_root: str = "/proc", limit: int = 10) -> List[Tuple[int, str, str]]:
    """Best-effort lsof-style scan: (pid, comm, path) of this host's processes with an fd or cwd under root."""
    root = os.path.realpath(root)
    me = os.getpid()
    hits: List[Tuple[int, str, str]] = []
    try:
        pids = [int(p) for p in os.listdir(proc_root) if p.isdigit()]
    except OSError:
        return hits
    for pid in pids:
        if pid == me:
            continue
        base = os.path.join(proc_root, str(pid))
        links = [os.path.join(base, "cwd")]
        try:
            links += [os.path.join(base, "fd", fd) for fd in os.listdir(os.path.join(base, "fd"))]
        except OSError:
            pass  # other users' processes: not readable
        for link in links:
            try:
                target = os.readlink(link)
            except OSError:
                continue
            target = target[:-len(" (deleted)")] if target.endswith(" (deleted)") else target
            if _inside(target, root):
                try:
                    with open(os.path.join(base, "comm")) as fh:
                        comm = fh.read().strip()
                except OSError:
                    comm = "?"
                hits.append((pid, comm, target))
                break
        if len(hits) >= limit:
            break
    return hits


def nice_prefix() -> List[str]:
    pre: List[str] = []
    if shutil.which("ionice"):
        pre += ["ionice", "-c3"]
    if shutil.which("nice"):
        pre += ["nice", "-n", "19"]
    return pre


def copy_command(src: str, dst: str, bwlimit: Optional[int]) -> List[str]:
    cmd = nice_prefix() + ["rsync", "-a"]
    if bwlimit:
        cmd.append(f"--bwlimit={int(bwlimit)}")
    return cmd + [src.rstrip("/") + "/", dst.rstrip("/") + "/"]


def checksum_command(src: str, dst: str) -> List[str]:
    return nice_prefix() + ["rsync", "-a", "--checksum", "--dry-run", "--itemize-changes",
                            src.rstrip("/") + "/", dst.rstrip("/") + "/"]


def finish_line(src: str, dst: str) -> str:
    s = os.path.abspath(src.rstrip("/") or "/")
    d = os.path.abspath(dst.rstrip("/") or "/")
    return f"cdx-agent --move-verify {shlex.quote(s)} {shlex.quote(d)} --verify-only --finish"


def finish(src: str, dst: str) -> int:
    """Trash the verified source, then link it to the copy. Refusals keep SRC in place."""
    from cdx_agent import trash
    s = os.path.abspath(src.rstrip("/") or "/")
    d = os.path.abspath(dst.rstrip("/") or "/")
    rc = trash.rm([s], verified_copy=True)
    if rc != 0 or os.path.lexists(s):
        print(f"NOT FINISHED: {s} was not trashed (see above); the copy at {d} is verified and kept")
        return EXIT_REFUSED
    os.symlink(d, s)
    print(f"FINISHED: {s} -> {d} (source in trash: cdx-agent --trash list)")
    return EXIT_OK


def user_line(src: str, dst: str) -> str:
    s = os.path.abspath(src.rstrip("/") or "/")
    d = os.path.abspath(dst.rstrip("/") or "/")
    return f"! rm -rf {shlex.quote(s)} && ln -s {shlex.quote(d)} {shlex.quote(s)}"


def verify(src: str, dst: str) -> Tuple[bool, List[str]]:
    msgs: List[str] = []
    r = subprocess.run(checksum_command(src, dst), capture_output=True, text=True)
    changes = [ln for ln in r.stdout.splitlines() if ln.strip()]
    ok = True
    if r.returncode != 0:
        ok = False
        msgs.append(f"checksum dry-run failed rc={r.returncode}: {r.stderr.strip()[-500:]}")
    if changes:
        ok = False
        msgs.append(f"checksum dry-run lists {len(changes)} difference(s):")
        msgs += ["  " + ln for ln in changes[:20]]
        if len(changes) > 20:
            msgs.append(f"  ... {len(changes) - 20} more")
    s, d = tree_stats(src), tree_stats(dst)
    for field in ("files", "bytes", "symlinks", "dirs", "other"):
        a, b = getattr(s, field), getattr(d, field)
        if a != b:
            ok = False
            msgs.append(f"{field} differ: SRC {a} vs DST {b}")
    if ok:
        msgs.append(f"{s.files} files, {s.bytes} bytes ({human(s.bytes)}), {s.symlinks} symlinks, {s.dirs} dirs, "
                    f"{s.other} other; checksum dry-run clean")
    return ok, msgs


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="cdx-agent --move-verify", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("src")
    p.add_argument("dst")
    p.add_argument("--bwlimit", type=int, help="rsync --bwlimit in KB/s")
    p.add_argument("--no-lsof", action="store_true", help="skip the open-files check under SRC")
    p.add_argument("--verify-only", action="store_true", help="skip the copy; only verify an earlier one")
    p.add_argument("--finish", action="store_true", help="after VERIFIED: trash SRC and symlink SRC -> DST")
    args = p.parse_args(argv)
    if not shutil.which("rsync"):
        print("REFUSED: rsync not found on PATH", file=sys.stderr)
        return EXIT_REFUSED
    reason = check_paths(args.src, args.dst)
    if reason:
        print(f"REFUSED: {reason}", file=sys.stderr)
        return EXIT_REFUSED
    src = os.path.abspath(args.src.rstrip("/"))
    dst = os.path.abspath(args.dst.rstrip("/"))
    if not args.no_lsof:
        hits = open_under(src)
        if hits:
            print(f"REFUSED: files under {src} are open on this host (close them, or --no-lsof):", file=sys.stderr)
            for pid, comm, path in hits:
                print(f"  pid {pid} ({comm}): {path}", file=sys.stderr)
            return EXIT_REFUSED
    if args.verify_only:
        if not os.path.isdir(dst):
            print(f"REFUSED: --verify-only but DST {dst} does not exist", file=sys.stderr)
            return EXIT_REFUSED
    else:
        os.makedirs(dst, exist_ok=True)
        cmd = copy_command(src, dst, args.bwlimit)
        print("copy: " + " ".join(shlex.quote(c) for c in cmd), flush=True)
        r = subprocess.run(cmd)
        if r.returncode != 0:
            print(f"NOT VERIFIED: rsync copy failed rc={r.returncode} (24 = source files vanished during the copy)")
            return EXIT_FAIL
    print("verify: " + " ".join(shlex.quote(c) for c in checksum_command(src, dst)) + " + file counts/bytes",
          flush=True)
    ok, msgs = verify(src, dst)
    if not ok:
        print(f"NOT VERIFIED: {src} -> {dst}")
        for m in msgs:
            print(m)
        return EXIT_FAIL
    print(f"VERIFIED {src} -> {dst}: {msgs[-1]}")
    if not args.finish:
        print("Nothing was deleted. To finish (trash SRC, restorable; symlink SRC -> DST):")
        print(finish_line(src, dst))
        return EXIT_OK
    return finish(src, dst)


if __name__ == "__main__":
    raise SystemExit(main())
