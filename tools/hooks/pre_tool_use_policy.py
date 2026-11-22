#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any


ADVISORY_STATE_FILENAME = ".pre_policy_advisory_state.json"
MAX_ADVISORY_SESSIONS = 200


# Deletes (rm/find -delete/git clean/...) are tiered by delete_guard.py (WP3); only non-delete destruction here.
# Checked against executed text only (heredoc bodies and non-executed quotes stripped).
BLOCK_PATTERNS = (
    re.compile(r"git\s+reset\s+--hard(\s|$)"),
)

WARN_PATTERNS = (
    re.compile(r"(^|[;&|]\s*)(pip|pip3|python\s+-m\s+pip)\s+install(\s|$)"),
    re.compile(r"(^|[;&|]\s*)(conda|mamba)\s+install(\s|$)"),
    re.compile(r"(^|[;&|]\s*)(chmod|chown)\s+-R(\s|$)"),
)

RISKY_PATH_HINTS = (
    "checkpoint",
    "checkpoints",
    "dataset",
    "datasets",
    "logs",
    "wandb",
    "outputs",
)

TOKEN_RISK_PATTERNS = (
    (re.compile(r"(^|[;&|]\s*)ls\s+-R(\s|$)"), "Recursive directory listings are token-expensive."),
    (re.compile(r"(^|[;&|]\s*)find\s+(\.|\S+)(\s|$)"), "Broad find output is token-expensive; prefer narrowed search or cdx-agent --context-budget."),
    (re.compile(r"(^|[;&|]\s*)(grep\s+-R|grep\s+--recursive)(\s|$)"), "Recursive grep is token-expensive; prefer rg with path filters."),
    (re.compile(r"(^|[;&|]\s*)git\s+diff(\s|$)"), "Full git diff can be expensive; prefer git diff --stat first."),
)


def _load_payload() -> dict[str, Any]:
    raw = sys.stdin.read()
    return json.loads(raw) if raw.strip() else {}


def _tool_command(payload: dict[str, Any]) -> str:
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        for key in ("command", "cmd", "patch", "text"):
            value = tool_input.get(key)
            if isinstance(value, str):
                return value
    if isinstance(tool_input, str):
        return tool_input
    return ""


def _looks_like_broad_recursive_permission_change(command: str) -> bool:
    command = command.strip()
    if not re.search(r"(^|[;&|]\s*)(chmod|chown)\s+-R(\s|$)", command):
        return False
    return any(
        token in command
        for token in (" /", " .", "$HOME", "/common/users/ss5772", "/common/home/ss5772")
    )


def _apply_patch_targets(command: str) -> list[str]:
    targets: list[str] = []
    for line in command.splitlines():
        if line.startswith("*** Update File: ") or line.startswith("*** Add File: ") or line.startswith("*** Delete File: "):
            targets.append(line.split(": ", 1)[1].strip())
    return targets


def _risky_targets_text(command: str) -> str | None:
    targets = _apply_patch_targets(command)
    risky = [target for target in targets if any(hint in target.lower() for hint in RISKY_PATH_HINTS)]
    if risky:
        return ", ".join(risky[:8])
    if any(hint in command.lower() for hint in RISKY_PATH_HINTS):
        return "command text references data/log/checkpoint paths"
    return None


# Waiting in a foreground shell burns an LLM turn for nothing (2026-10-17: an agent's `until ! pgrep -f "pytest..."` matched
# itself and looped 7 min). Background runs (run_in_background) notify on exit and are free, so only foreground waits are denied.
WAIT_PID = re.compile(r"\btail\s+(-\S+\s+)*--pid[=\s]+\S|(^|[;&|]\s*)wait\s+(%\d+|\$\{?\w+\}?|\d+)(\s|;|$)|(^|[;&|]\s*)inotifywait\b")   # foreground wait on another process
LOOP = re.compile(r"\b(?P<kw>until|while)\b(?P<cond>.*?)[;\n]\s*do\b(?P<body>.*?)\bdone\b", re.S)   # single- or multi-line
POLL_COND = re.compile(r"\b(test|grep\s+-\w*q|pgrep|kill\s+-0|ls|stat|squeue|sacct|curl|true)\b|\[|(^|\s):(\s|$)")
BOUNDED_PREFIX = re.compile(r"\btimeout\s+(?:-\S+\s+)*(\d+)([smh]?)\s+(?:bash|sh)\s+-c\s+['\"]\s*$")
PGREP_F = re.compile(r"\bp(?:grep|kill)\s+((?:-\S+\s+)*)(?:(['\"])(.*?)\2|(\S+))")
MAX_BOUNDED_WAIT_S = 120


def _foreground_wait_loop(command: str) -> bool:
    for m in LOOP.finditer(command):
        if not re.search(r"\bsleep\b", m["body"]) or not POLL_COND.search(m["cond"]):
            continue
        bound = BOUNDED_PREFIX.search(command[:m.start()])
        if bound and int(bound[1]) * {"": 1, "s": 1, "m": 60, "h": 3600}[bound[2]] <= MAX_BOUNDED_WAIT_S:
            continue
        return True
    return False


def _self_matching_pgrep(command: str) -> bool:
    """`pgrep/pkill -f PAT` where PAT (as a regex) matches the command text: it also matches the shell running this command."""
    blanked = _strip_nonexecuted_quotes(command)  # same length; quoted prose (commit messages, echo) is blanked
    heredocs = [h.span() for h in HEREDOC.finditer(command)]
    for m in PGREP_F.finditer(command):
        word = m.start() + len(m[0]) - len(m[0].lstrip())
        if not blanked[word:word + 1].strip() or any(a <= word < b for a, b in heredocs):
            continue
        if not any(f.startswith("-") and not f.startswith("--") and "f" in f for f in m[1].split()):
            continue
        pat = m[3] if m[2] else m[4]
        try:
            if re.search(pat, command):
                return True
        except re.error:
            if pat in command:
                return True
    return False


HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n\s*\2\s*(\n|$)", re.S)


def _shell_segments(command: str) -> list[list[dict[str, Any]]]:
    """Return shell-ish words and quote spans, without executing anything."""
    segments: list[list[dict[str, Any]]] = []
    words: list[dict[str, Any]] = []
    value: list[str] = []
    quote_spans: list[tuple[int, int]] = []
    word_start: int | None = None
    i = 0

    def flush_word() -> None:
        nonlocal value, quote_spans, word_start
        if word_start is not None:
            words.append({"value": "".join(value), "quotes": quote_spans})
        value = []
        quote_spans = []
        word_start = None

    def flush_segment() -> None:
        nonlocal words
        flush_word()
        if words:
            segments.append(words)
        words = []

    while i < len(command):
        char = command[i]
        if char.isspace():
            flush_word()
            i += 1
            continue
        if char in ";|&\n":
            flush_segment()
            while i < len(command) and command[i] in ";|&\n":
                i += 1
            continue
        if word_start is None:
            word_start = i
        if char == "\\":
            if i + 1 < len(command):
                value.append(command[i + 1])
                i += 2
            else:
                i += 1
            continue
        if char in "'\"":
            quote = char
            quote_start = i + 1
            i += 1
            while i < len(command):
                if quote == '"' and command[i] == "\\" and i + 1 < len(command):
                    value.append(command[i + 1])
                    i += 2
                    continue
                if command[i] == quote:
                    break
                value.append(command[i])
                i += 1
            quote_end = i
            quote_spans.append((quote_start, quote_end))
            if i < len(command):
                i += 1
            continue
        value.append(char)
        i += 1
    flush_segment()
    return segments


def _word_basename(value: str) -> str:
    return value.rsplit("/", 1)[-1]


def _quote_spans_to_execute(segments: list[list[dict[str, Any]]]) -> set[tuple[int, int]]:
    """Keep quoted text only where the shell will execute it as a command."""
    preserve: set[tuple[int, int]] = set()
    for words in segments:
        values = [str(word["value"]) for word in words]
        for index, value in enumerate(values):
            if value == "eval":
                for word in words[index + 1:]:
                    preserve.update(word["quotes"])
            if _word_basename(value) in {"bash", "sh", "zsh"} and index + 1 < len(values):
                if values[index + 1] == "-c":
                    if index + 2 < len(words):
                        preserve.update(words[index + 2]["quotes"])
            if _word_basename(value) == "ssh":
                host_index = index + 1
                option_args = {"-b", "-c", "-F", "-i", "-J", "-L", "-l", "-o", "-p", "-R", "-W"}
                while host_index < len(values):
                    option = values[host_index]
                    if option == "--":
                        host_index += 1
                        break
                    if not option.startswith("-"):
                        break
                    host_index += 2 if option in option_args and host_index + 1 < len(values) else 1
                for word in words[host_index + 1:]:
                    preserve.update(word["quotes"])
    return preserve


def _strip_nonexecuted_quotes(command: str) -> str:
    segments = _shell_segments(command)
    preserve = _quote_spans_to_execute(segments)
    stripped = list(command)
    for words in segments:
        for word in words:
            for start, end in word["quotes"]:
                if (start, end) in preserve:
                    continue
                for index in range(start, min(end, len(stripped))):
                    stripped[index] = " "
    return "".join(stripped)


def _strip_heredocs(command: str) -> str:
    """Heredoc bodies are file contents being written (e.g. a watcher script), not commands this call runs."""
    return _strip_nonexecuted_quotes(HEREDOC.sub("\n", command))


def poll_guard(command: str, background: bool) -> str | None:
    """Reason to deny, or None. Foreground wait loops, and pgrep/pkill -f regexes that can match their own command line."""
    if _self_matching_pgrep(command):
        return ("Blocked by cdx-agent hook: `pgrep/pkill -f '<pattern>'` matches its own command line / the shell running it (it did, looping forever). "
                "Use the bracket trick (pgrep -f '[p]ytest ...') or a PID.")
    if not background and (_foreground_wait_loop(_strip_heredocs(command)) or WAIT_PID.search(_strip_heredocs(command))):
        return ("Blocked by cdx-agent hook: foreground wait/poll loop. Run the job in the foreground with a timeout and read its output, "
                "or launch it detached and hand the waiting back (main session: run_in_background shell watcher; agents: report and stop).")
    return None


def _repo_root(cwd_value: Any) -> Path | None:
    if isinstance(cwd_value, str) and cwd_value:
        candidate = Path(cwd_value)
        if not candidate.exists():
            candidate = Path.cwd()
    else:
        candidate = Path.cwd()
    try:
        candidate = candidate.resolve()
        if candidate.is_file():
            candidate = candidate.parent
        for directory in (candidate, *candidate.parents):
            if (directory / ".git").exists():
                return directory
    except (OSError, RuntimeError):
        return None
    return None


def _common_cdx_dir(root: Path) -> Path:
    """The main checkout's .cdx/ (via `git rev-parse --git-common-dir`), for worktrees that have none. Falls back to <root>/.cdx."""
    try:
        result = subprocess.run(["git", "-C", str(root), "rev-parse", "--git-common-dir"],
                                capture_output=True, text=True, timeout=2, check=False)
        text = result.stdout.strip()
        if result.returncode == 0 and text:
            common = Path(text)
            if not common.is_absolute():
                common = root / common
            return common.resolve().parent / ".cdx"
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return root / ".cdx"


def _hardware_verdict(command: str, payload: dict[str, Any]) -> str | None:
    """`<repo>/.cdx/hardware.json` {"patterns": [regex...], "reason": "..."}: a Bash command that matches asks the user
    (WP10; standing rule: no real robot/arm/gripper/motor actuation without approval in the current session)."""
    root = _repo_root(payload.get("cwd"))
    if root is None:
        return None
    for cdx in (root / ".cdx", _common_cdx_dir(root)):
        f = cdx / "hardware.json"
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        for pat in data.get("patterns") or []:
            try:
                if re.search(pat, command):
                    return (f"cdx-agent: this command matches a hardware pattern in {f} ({pat!r}). "
                            f"{data.get('reason') or 'It can move or power real hardware.'} The user approves it in this session.")
            except re.error:
                continue
        return None
    return None


def _ask(reason: str) -> int:
    if os.environ.get("CDX_ENGINE") == "codex":   # Codex has no "ask"
        return _deny(reason)
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
                                             "permissionDecisionReason": reason}}))
    return 0


LONG_SUITE_TAGS = {"local-only"}   # `tests/test_x.py local-only`: needs workstation-local data, never send it to Slurm


def _long_suite_patterns(root: Path) -> list[tuple[str, bool]]:
    """-> [(glob, local_only)] from <repo>/.cdx/long_suites.txt (one glob per line, optional trailing tag `local-only`)."""
    path = root / ".cdx" / "long_suites.txt"
    try:
        if not path.is_file():
            path = _common_cdx_dir(root) / "long_suites.txt"
        if not path.is_file():
            return []
        patterns = []
        for line in path.read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            tokens = line.split()
            tags = {token for token in tokens[1:] if token in LONG_SUITE_TAGS}
            glob = " ".join(token for index, token in enumerate(tokens) if index == 0 or token not in LONG_SUITE_TAGS)
            patterns.append((glob, "local-only" in tags))
        return [item for item in patterns if item[0]]
    except (OSError, UnicodeError):
        return []


def _long_suite_message(label: str, local_only: bool, command: str) -> str:
    if not local_only:
        return f"Long test suite {label}: run it via `cdx-agent --cpu-job --wait -- <cmd>` (ilab) or prefix CDX_ALLOW_LONG_SUITE=1 if you must run it here."
    simple = "\n" not in command and not re.search(r"[;&|]", command)
    cmd = command.strip() if simple else "<cmd>"
    return (f"Long test suite {label} is tagged local-only (its data is not readable from Slurm nodes, so --cpu-job would fail): "
            "run it on this machine in the background (run_in_background, do not wait on it), niced with capped threads: "
            f"`CDX_ALLOW_LONG_SUITE=1 OMP_NUM_THREADS=2 MKL_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 CUDA_VISIBLE_DEVICES= nice -n 10 timeout 3600 {cmd}`.")


def _pytest_invocations(command: str) -> list[tuple[str, list[str]]]:
    invocations: list[tuple[str, list[str]]] = []
    for executable, arguments in _runtime_invocations(command):
        if executable == "pytest":
            invocations.append((executable, arguments))
        elif re.fullmatch(r"python(?:\d+(?:\.\d+)?)?", executable):
            if len(arguments) >= 2 and arguments[0] == "-m" and arguments[1] == "pytest":
                invocations.append((executable, arguments[2:]))
    return invocations


PYTEST_OPTION_ARGUMENTS = {
    "-c", "-k", "-m", "--basetemp", "--confcutdir", "--cov", "--deselect", "--ignore",
    "--junitxml", "--log-file", "--override-ini", "--rootdir", "--tb", "--trace-config",
}


def _pytest_paths(arguments: list[str]) -> list[str]:
    paths: list[str] = []
    skip_next = False
    after_separator = False
    for argument in arguments:
        if skip_next:
            skip_next = False
            continue
        if argument == "--":
            after_separator = True
            continue
        if not after_separator and argument.startswith("-"):
            if argument in PYTEST_OPTION_ARGUMENTS:
                skip_next = True
            continue
        paths.append(argument)
    return paths


def _pattern_matches_repo(root: Path, pattern: str) -> bool:
    try:
        return any(root.glob(pattern))
    except (OSError, ValueError, NotImplementedError):
        return False


def _path_matches_pattern(root: Path, path_text: str, pattern: str) -> bool:
    path_text = path_text.split("::", 1)[0]
    if not path_text:
        return False
    try:
        path = Path(path_text)
        if path.is_absolute():
            relative = path.resolve().relative_to(root.resolve()).as_posix()
        else:
            relative = path.as_posix().lstrip("./")
        if fnmatchcase(relative, pattern) or fnmatchcase(relative, pattern.lstrip("./")):
            return True
        target = (root / relative).resolve()
        for match in root.glob(pattern):
            try:
                match.resolve().relative_to(target)
                return True
            except ValueError:
                continue
    except (OSError, RuntimeError, ValueError, NotImplementedError):
        return False
    return False


def _remote_long_suite_guard(command: str, root: Path, patterns: list[tuple[str, bool]]) -> str | None:
    """pytest inside `ssh HOST ...` / `srun ...`: deny only for local-only suites (plain long suites are fine remotely)."""
    local_patterns = [item for item in patterns if item[1]]
    if not local_patterns:
        return None
    for match in re.finditer(r"\bpytest\b", command):
        tail = re.split(r"[;&|\n]", command[match.end():], maxsplit=1)[0]
        tokens = [token.strip("'\"") for token in tail.split()]
        paths = _pytest_paths([token for token in tokens if token])
        candidates = paths or ["."]
        for path in candidates:
            for pattern, _local in local_patterns:
                if path == "." and _pattern_matches_repo(root, pattern):
                    return _remote_local_only_message(pattern)
                if path != "." and _path_matches_pattern(root, path, pattern):
                    return _remote_local_only_message(path)
    return None


def _remote_local_only_message(label: str) -> str:
    return (f"Long test suite {label} is tagged local-only (its data is not readable from the remote host), so it must not run via ssh/srun: "
            "run it on this machine in the background, niced with capped threads, or prefix CDX_ALLOW_LONG_SUITE=1 if you must.")


def _long_suite_guard(command: str, cwd_value: Any) -> str | None:
    if re.search(r"\bcdx-agent\s+--cpu-job\b", command):
        return None
    if re.search(r"\bpython(?:\d+(?:\.\d+)?)?\s+-m\s+cdx_agent\.cpu_job\b", command):
        return None
    if re.search(r"(?<!\w)CDX_ALLOW_LONG_SUITE=1(?!\w)", command):
        return None
    root = _repo_root(cwd_value)
    if root is None:
        return None
    patterns = _long_suite_patterns(root)
    if not patterns:
        return None
    if re.search(r"\bssh\s+\S+[\s\S]*?\bpytest\b", command) or re.search(r"\bsrun\b[\s\S]*?\bpytest\b", command):
        return _remote_long_suite_guard(command, root, patterns)
    for _executable, arguments in _pytest_invocations(command):
        if "--version" in arguments or "-V" in arguments:
            continue
        paths = _pytest_paths(arguments)
        if paths:
            for path in paths:
                for pattern, local_only in patterns:
                    if _path_matches_pattern(root, path, pattern):
                        return _long_suite_message(path, local_only, command)
        else:
            for pattern, local_only in patterns:
                if _pattern_matches_repo(root, pattern):
                    return _long_suite_message(pattern, local_only, command)
    return None


def _executed_command_strings(command: str) -> list[str]:
    nested: list[str] = []
    for words in _shell_segments(command):
        values = [str(word["value"]) for word in words]
        for index, value in enumerate(values):
            if _word_basename(value) in {"bash", "sh", "zsh"} and index + 2 < len(words) and values[index + 1] == "-c":
                nested.append(values[index + 2])
            elif value == "eval":
                nested.append(" ".join(values[index + 1:]))
    return nested


def _runtime_invocations(command: str, depth: int = 0) -> list[tuple[str, list[str]]]:
    """Find the executable at each simple shell command's head."""
    found: list[tuple[str, list[str]]] = []
    for words in _shell_segments(_strip_heredocs(command)):
        values = [str(word["value"]) for word in words]
        index = 0
        while index < len(values) and ("=" in values[index] and not values[index].startswith("-")):
            index += 1
        while index < len(values):
            base = _word_basename(values[index])
            if base == "env":
                index += 1
                while index < len(values) and (values[index].startswith("-") or ("=" in values[index] and not values[index].startswith("-"))):
                    index += 1
                continue
            if base == "timeout":
                index += 1
                while index < len(values) and (values[index].startswith("-") or re.fullmatch(r"\d+(?:\.\d+)?[smhd]?", values[index])):
                    index += 1
                continue
            if base == "srun":
                index += 1
                while index < len(values) and values[index].startswith("-"):
                    index += 2 if index + 1 < len(values) and values[index] in {"-c", "-J", "-N", "-n", "-p", "--cpus-per-task", "--gres", "--job-name", "--partition", "--time"} else 1
                continue
            if base in {"nohup", "command", "exec", "sudo"}:
                index += 1
                continue
            if base == "pytest" or re.fullmatch(r"python(?:\d+(?:\.\d+)?)?", base):
                found.append((base, values[index + 1:]))
            break
    if depth < 2:
        for nested in _executed_command_strings(command):
            found.extend(_runtime_invocations(nested, depth + 1))
    return found


def _load_advisory(command: str) -> str | None:
    if re.search(r"\bcdx-agent\s+--cpu-job\b", command) or re.search(
        r"\bpython(?:\d+(?:\.\d+)?)?\s+-m\s+cdx_agent\.cpu_job\b", command
    ):
        return None
    invocations = _runtime_invocations(command)
    active = False
    raw_print_one_liner = bool(re.search(
        r"\bpython(?:\d+(?:\.\d+)?)?\b[^;&|\n]*\s-c\s*(['\"]?)\s*print\b", command
    ))
    for executable, arguments in invocations:
        if executable == "pytest":
            if "--version" not in arguments and "-V" not in arguments:
                active = True
        else:
            if any(argument in {"--version", "-V"} for argument in arguments):
                continue
            if "-m" in arguments:
                module_index = arguments.index("-m")
                if module_index + 1 < len(arguments) and arguments[module_index + 1] == "pip":
                    continue
            if "-c" in arguments and raw_print_one_liner:
                continue
            active = True
    if not active:
        return None
    load_text = os.environ.get("CDX_TEST_LOAD")
    cpu_text = os.environ.get("CDX_TEST_CPUS")
    load = float(load_text) if load_text is not None else os.getloadavg()[0]
    cpus = int(cpu_text) if cpu_text is not None else (os.cpu_count() or 1)
    if cpus <= 0 or load <= cpus:
        return None
    message = f"Machine overloaded (load {load:g} on {cpus} cores): move CPU-heavy work to ilab with `cdx-agent --cpu-job`; keep threads capped."
    return message


def _deny(reason: str) -> int:
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    print(json.dumps(payload))
    return 0


def _context(message: str) -> int:
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": message,
        }
    }
    print(json.dumps(payload))
    return 0


def _advisory_state_path() -> Path:
    return Path(sys.argv[0]).absolute().parent / ADVISORY_STATE_FILENAME


def _read_advisory_state() -> dict[str, list[str]]:
    try:
        data = json.loads(_advisory_state_path().read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(session): [str(item) for item in messages if isinstance(item, str)]
        for session, messages in data.items()
        if isinstance(messages, list)
    }


def _write_advisory_state(state: dict[str, list[str]]) -> None:
    state = dict(list(state.items())[-MAX_ADVISORY_SESSIONS:])
    path = _advisory_state_path()
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(state))
        os.replace(temporary, path)
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass


def _new_advisories(session_id: str, messages: list[str], dedupe_seconds: float | None = None) -> list[str]:
    state = _read_advisory_state()
    seen = state.get(session_id, [])
    now = time.time()

    def entry_digest(entry: str) -> str:
        return entry.split("|", 1)[0]

    def entry_is_recent(entry: str, digest: str) -> bool:
        if entry_digest(entry) != digest:
            return False
        if dedupe_seconds is None or "|" not in entry:
            return True
        try:
            return now - float(entry.split("|", 1)[1]) < dedupe_seconds
        except (TypeError, ValueError):
            return True

    fresh: list[str] = []
    for message in messages:
        digest = hashlib.sha256(message.encode()).hexdigest()
        if not any(entry_is_recent(entry, digest) for entry in seen):
            fresh.append(message)
            seen.append(f"{digest}|{now:.6f}" if dedupe_seconds is not None else digest)
    if fresh:
        state.pop(session_id, None)
        state[session_id] = seen[-200:]
        _write_advisory_state(state)
    return fresh


def _command_paths(command: str) -> list[str]:
    return re.findall(r"(?:^|\s)([./~]?[A-Za-z0-9_./-]+\.[A-Za-z0-9_]+)", command)


def _token_risk_warnings(command: str) -> list[str]:
    warnings: list[str] = []
    stripped = command.strip()
    for pattern, message in TOKEN_RISK_PATTERNS:
        if pattern.search(stripped):
            if stripped.startswith("git diff") and ("--stat" in stripped or "--name-only" in stripped):
                continue
            if stripped.startswith("find ") and ("-maxdepth" in stripped or " -name " in stripped or " -path " in stripped):
                continue
            warnings.append(message)
    if stripped.startswith("cat "):
        for path in _command_paths(stripped):
            lower = path.lower()
            if any(hint in lower for hint in RISKY_PATH_HINTS):
                warnings.append("Reading files from logs/checkpoints/datasets/outputs directly is token-expensive.")
                break
    if stripped.startswith(("rg ", "grep ")):
        if " ./" in f" {stripped}" or stripped.endswith(" .") or stripped.endswith(" ./") or stripped.endswith(" ./*"):
            warnings.append("Search scope looks broad; narrow the query or use safe_rg.py.")
    if any(f"/{hint}/" in f"/{stripped.lower()}/" for hint in ("logs", "checkpoints", "datasets", "wandb", "runs", "outputs", "videos")):
        warnings.append("Command targets generated artifacts; summarize first unless raw output is required.")
    return warnings


def _slurm_verdict(command: str) -> str | None:
    """Raw sbatch/srun deny (slurm_guard.py, user ruling Q6). An import failure allows (advisory-level rule)."""
    here = os.path.dirname(os.path.realpath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        import slurm_guard
        return slurm_guard.check(command)
    except Exception:
        return None


def _delete_verdict(command: str, payload: dict[str, Any]):
    """Deletion tiers (delete_guard.py beside this file's real path). An import failure denies deletes only."""
    here = os.path.dirname(os.path.realpath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        import delete_guard
    except Exception:
        if re.search(r"(^|[\s;&|(])(rm|rmdir|unlink|shred)\s|-delete\b|git\s+clean\b", command):
            from types import SimpleNamespace
            return SimpleNamespace(action="deny", reason="cdx-agent delete policy unavailable; use `cdx-agent --rm PATH...`.")
        return None
    ti = payload.get("tool_input") or {}
    return delete_guard.check(command, str(payload.get("cwd") or os.getcwd()),
                              session_id=str(payload.get("session_id") or ""),
                              agent_id=str(payload.get("agent_id") or (ti.get("agent_id") if isinstance(ti, dict) else "") or ""))


def main() -> int:
    payload = _load_payload()
    tool_name = str(payload.get("tool_name", ""))
    if tool_name not in {"Bash", "apply_patch"}:
        return 0

    command = _tool_command(payload)
    if not command:
        return 0

    executed = _strip_heredocs(_strip_nonexecuted_quotes(command))
    for pattern in BLOCK_PATTERNS:
        if pattern.search(executed):
            return _deny("Blocked by cdx-agent hook: destructive command pattern (git reset --hard).")

    if tool_name == "Bash":
        verdict = _delete_verdict(command, payload)
        if verdict is not None and verdict.action == "deny":
            return _deny(verdict.reason)
        slurm = _slurm_verdict(command)
        if slurm:
            return _deny(slurm)
        hw = _hardware_verdict(command, payload)
        if hw:
            return _ask(hw)

    ti = payload.get("tool_input") or {}
    poll = poll_guard(command, bool(ti.get("run_in_background")) if isinstance(ti, dict) else False)
    if poll:
        return _deny(poll)

    if tool_name == "Bash":
        long_suite = _long_suite_guard(command, payload.get("cwd"))
        if long_suite:
            return _deny(long_suite)

    if _looks_like_broad_recursive_permission_change(command):
        return _deny("Blocked by cdx-agent hook: broad recursive chmod/chown.")

    risky_targets = _risky_targets_text(command)
    safety_warnings: list[str] = []
    if risky_targets:
        safety_warnings.append(f"Command may touch generated or sensitive paths: {risky_targets}.")
    for pattern in WARN_PATTERNS:
        if pattern.search(command):
            safety_warnings.append("Command installs packages or changes broad permissions; confirm intent.")
            break
    token_warnings = _token_risk_warnings(command)
    load_message = _load_advisory(command) if tool_name == "Bash" else None
    messages: list[str] = []
    if safety_warnings:
        messages.append("Safety: " + " ".join(safety_warnings))
    if token_warnings:
        messages.append("Token risk: " + " ".join(token_warnings))
    session_id = str(payload.get("session_id") or "unknown-session")
    fresh_messages = _new_advisories(session_id, messages) if messages else []
    if load_message and _new_advisories(session_id, [load_message], dedupe_seconds=3600):
        fresh_messages.append(load_message)
    if fresh_messages:
        return _context(" ".join(fresh_messages))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        # A crashing hook stalls the agent's tool loop; never crash or emit partial output.
        raise SystemExit(0)
