"""Skill discovery, linking, and enforced audit gating.

Fixes the confirmed **A4** safety bug in the bash predecessor: skills were
regex-audited for risky instructions (`--skills-audit`, backed by
`codex_tools/token_tools/validate_skills.py`), but the audit was purely a
manual, on-demand report -- a skill with a `critical` finding in
`skills_approved`/`skills_custom` was still symlinked into every runtime and
auto-loaded; only `skills_quarantine` was ever excluded from linking, and that
exclusion had nothing to do with audit results. Here, `link_skill_root` runs
the same audit *at link time*, for every root, and by default refuses to link
any skill with a `critical` finding unless it has been explicitly allowlisted.
A `warning` finding still links, but the decision records it so a caller can
surface a banner.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .config import Config, backup_path, load_config, repo_root

DEFAULT_DESC_MIN_WORDS = 6
DEFAULT_DESC_MAX_CHARS = 220
AUDIT_CACHE_FILENAME = "skill_audit_cache.json"

AUDIT_RULES: tuple[tuple[str, str, str, re.Pattern], ...] = (
    ("critical", "curl download", "curl + URL in a skill command", re.compile(r"\bcurl\b.*https?://", re.IGNORECASE)),
    ("critical", "wget download", "wget + URL in a skill command", re.compile(r"\bwget\b.*https?://", re.IGNORECASE)),
    ("critical", "sudo", "skill text contains sudo", re.compile(r"\bsudo\b", re.IGNORECASE)),
    ("critical", "rm -rf", "skill text contains destructive removal", re.compile(r"\brm\s+-rf\b", re.IGNORECASE)),
    (
        "critical",
        "npm install -g",
        "skill text contains global npm install",
        re.compile(r"\bnpm\s+install\s+-g\b", re.IGNORECASE),
    ),
    (
        "critical",
        "pip install",
        "skill text contains a pip install variant",
        re.compile(r"\b(?:pip3?|python3?\s+-m\s+pip)\s+install\b", re.IGNORECASE),
    ),
    (
        "critical",
        "apt install",
        "skill text contains a system package install",
        re.compile(r"\bapt(?:-get)?\s+(?:-y\s+)?install\b", re.IGNORECASE),
    ),
    ("critical", "bash -c", "skill text contains bash -c", re.compile(r"\bbash\s+-c\b", re.IGNORECASE)),
    ("critical", "sh -c", "skill text contains sh -c", re.compile(r"\bsh\s+-c\b", re.IGNORECASE)),
    ("critical", "chmod 777", "skill text contains chmod 777", re.compile(r"\bchmod\s+777\b", re.IGNORECASE)),
    (
        "critical",
        "composio login",
        "skill text contains composio login",
        re.compile(r"\bcomposio\s+login\b", re.IGNORECASE),
    ),
    ("warning", "gh auth", "skill text contains gh auth", re.compile(r"\bgh\s+auth\b", re.IGNORECASE)),
    (
        "warning",
        "eval",
        "skill text contains a shell-like eval usage",
        re.compile(r"^\s*(?:[-*]\s*)?eval(\s|\()", re.IGNORECASE),
    ),
    (
        "warning",
        "credential assignment",
        "skill text mentions a credential-like assignment",
        re.compile(r"\b(token|secret|password|api[_-]?key)\s*[:=]", re.IGNORECASE),
    ),
)


@dataclass(frozen=True)
class AuditFinding:
    severity: str
    rule_name: str
    message: str
    line_no: int
    line_text: str
    file: str = ""  # relative companion-file path; "" for SKILL.md itself


@dataclass(frozen=True)
class AuditResult:
    path: Path
    findings: tuple[AuditFinding, ...]

    @property
    def severity(self) -> Literal["critical", "warning", "clean"]:
        if any(f.severity == "critical" for f in self.findings):
            return "critical"
        if any(f.severity == "warning" for f in self.findings):
            return "warning"
        return "clean"


_NEGATION_RE = re.compile(r"\b(?:do\s+not|don'?t|never|avoid|not)\b[^.]*$", re.IGNORECASE)


def audit_skill_text(path: Path, text: str) -> AuditResult:
    findings = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        for severity, rule_name, message, pattern in AUDIT_RULES:
            match = pattern.search(line)
            if not match:
                continue
            effective = severity
            if severity == "critical" and _NEGATION_RE.search(line[: match.start()]):
                # Cautionary prose ("do NOT run pip install") is guidance, not
                # an instruction to execute -- hard-blocking it would punish
                # exactly the skills that warn against the dangerous pattern.
                effective = "warning"
            findings.append(AuditFinding(effective, rule_name, message, line_no, line.strip()))
    return AuditResult(path=path, findings=tuple(findings))


def audit_skill(path: Path) -> AuditResult:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        return AuditResult(path=path, findings=(AuditFinding("critical", "unreadable", str(exc), 0, ""),))
    return audit_skill_text(path, text)


# --- audit result cache (content-hash keyed, avoids re-scanning unchanged skills) --------


def _cache_path(config: Config) -> Path:
    return config.tools_root / ".cache" / AUDIT_CACHE_FILENAME


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _load_cache(config: Config) -> dict:
    path = _cache_path(config)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(config: Config, cache: dict) -> None:
    path = _cache_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=2, sort_keys=True))


def audit_skill_cached(config: Config, path: Path) -> AuditResult:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        return AuditResult(path=path, findings=(AuditFinding("critical", "unreadable", str(exc), 0, ""),))

    digest = _content_hash(text)
    cache = _load_cache(config)
    key = str(path)
    entry = cache.get(key)
    if entry is not None and entry.get("hash") == digest:
        findings = tuple(AuditFinding(**f) for f in entry.get("findings", []))
        return AuditResult(path=path, findings=findings)

    result = audit_skill_text(path, text)
    cache[key] = {
        "hash": digest,
        "findings": [
            {
                "severity": f.severity,
                "rule_name": f.rule_name,
                "message": f.message,
                "line_no": f.line_no,
                "line_text": f.line_text,
                "file": f.file,
            }
            for f in result.findings
        ],
        "audited_at": time.time(),
    }
    _save_cache(config, cache)
    return result


AUDITABLE_COMPANION_SUFFIXES = {".md", ".sh", ".py", ".yaml", ".yml", ".json", ".toml", ".bash", ".zsh"}
MAX_AUDITED_COMPANION_FILES = 50


def audit_skill_dir(config: Config, skill_dir: Path) -> AuditResult:
    """Audit the WHOLE skill directory, not just SKILL.md. The link gate
    symlinks the entire folder into the runtime, so a companion script
    (`helper.sh`, `agents/*.yaml`, stray `.bak` text) that never got scanned
    was a silent bypass of the A4 gate this module exists to enforce."""
    findings: list[AuditFinding] = []
    candidates = [skill_dir / "SKILL.md"]
    extras = sorted(
        p
        for p in skill_dir.rglob("*")
        if p.is_file() and p.name != "SKILL.md" and (p.suffix.lower() in AUDITABLE_COMPANION_SUFFIXES or ".bak" in p.name)
    )
    candidates.extend(extras[:MAX_AUDITED_COMPANION_FILES])
    for candidate in candidates:
        if not candidate.is_file():
            continue
        result = audit_skill_cached(config, candidate)
        rel = str(candidate.relative_to(skill_dir))
        for finding in result.findings:
            findings.append(
                AuditFinding(
                    finding.severity,
                    finding.rule_name,
                    finding.message,
                    finding.line_no,
                    finding.line_text,
                    file=rel if rel != "SKILL.md" else "",
                )
            )
    return AuditResult(path=skill_dir, findings=tuple(findings))


# --- per-engine scoping ------------------------------------------------------------


_ENGINE_BY_AGENT_STEM = {
    "openai": "codex",
    "codex": "codex",
    "claude": "claude",
    "anthropic": "claude",
}
ALL_ENGINES: tuple[str, ...] = ("claude", "codex")


def skill_engines(skill_dir: Path) -> tuple[str, ...]:
    """Which engines a skill is scoped to. A skill dir may carry
    `agents/<vendor>.yaml` files (e.g. `agents/openai.yaml`) declaring who it
    is meant for; previously that intent was silently ignored. No agents/
    dir, or no recognized vendor stems -> visible to all engines."""
    agents_dir = skill_dir / "agents"
    if not agents_dir.is_dir():
        return ALL_ENGINES
    engines: set[str] = set()
    for candidate in agents_dir.iterdir():
        if candidate.suffix.lower() not in {".yaml", ".yml"}:
            continue
        engine = _ENGINE_BY_AGENT_STEM.get(candidate.stem.lower())
        if engine:
            engines.add(engine)
    return tuple(sorted(engines)) if engines else ALL_ENGINES


# --- SKILL.md frontmatter parsing ------------------------------------------------------


@dataclass
class SkillMeta:
    name: str
    description: str
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not self.reasons


def parse_skill_frontmatter(path: Path) -> SkillMeta:
    text = path.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()
    reasons: list[str] = []
    warnings: list[str] = []
    name = path.parent.name
    description = ""

    if not lines or lines[0].strip() != "---":
        reasons.append("missing frontmatter")
        return SkillMeta(name=name, description=description, reasons=reasons, warnings=warnings)

    closing_index = None
    for idx, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            closing_index = idx
            break
    if closing_index is None:
        reasons.append("missing frontmatter")
        return SkillMeta(name=name, description=description, reasons=reasons, warnings=warnings)

    metadata: dict[str, str] = {}
    idx = 1
    while idx < closing_index:
        raw = lines[idx]
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or ":" not in raw:
            idx += 1
            continue
        key, value = raw.split(":", 1)
        key = key.strip().lower()
        value = value.strip().strip("'\"")
        if key in {"name", "description"} and value:
            metadata[key] = value
        idx += 1

    if not metadata.get("name"):
        reasons.append("missing name")
    else:
        name = metadata["name"]
        if name != path.parent.name:
            reasons.append(
                f"frontmatter name '{name}' does not match directory name '{path.parent.name}'"
            )
    if not metadata.get("description"):
        reasons.append("missing description")
    else:
        description = metadata["description"]

    if description:
        words = re.findall(r"[A-Za-z0-9_]+", description)
        if len(words) < DEFAULT_DESC_MIN_WORDS or len(description) < 32:
            warnings.append("description may be too vague")
        if len(description) > DEFAULT_DESC_MAX_CHARS:
            warnings.append("description is long; consider tightening it")

    return SkillMeta(name=name, description=description, reasons=reasons, warnings=warnings)


# --- root discovery + linking (A4 enforced-audit gate) ----------------------------------


def collect_runtime_skill_roots(config: Config, repo: Path) -> list[Path]:
    """Roots that are auto-linked into a runtime. `config.quarantine_root` is
    deliberately never included here -- unchanged behavior from bash."""
    return list(config.repo_skill_roots(repo))


LinkAction = Literal["linked", "unchanged", "blocked", "linked_with_warning", "skipped_engine"]


@dataclass(frozen=True)
class LinkDecision:
    skill_name: str
    source: Path
    action: LinkAction
    audit: AuditResult | None = None
    detail: str = ""


def link_skill_root(
    config: Config,
    src_root: Path,
    dst_root: Path,
    allowlist: frozenset[str] = frozenset(),
    engine: str | None = None,
) -> list[LinkDecision]:
    decisions: list[LinkDecision] = []
    if not src_root.is_dir():
        return decisions
    for skill_dir in sorted(p for p in src_root.iterdir() if p.is_dir()):
        skill_name = skill_dir.name
        skill_md = skill_dir / "SKILL.md"
        target = dst_root / skill_name

        if engine is not None and engine not in skill_engines(skill_dir):
            decisions.append(
                LinkDecision(
                    skill_name,
                    skill_dir,
                    "skipped_engine",
                    detail=f"scoped to {','.join(skill_engines(skill_dir))} via agents/*.yaml; engine is {engine}",
                )
            )
            continue

        # Whole-dir audit: the symlink exposes the entire folder, so every
        # companion file is part of the gate decision, not just SKILL.md.
        audit = audit_skill_dir(config, skill_dir) if skill_md.is_file() else None

        if audit is not None and audit.severity == "critical" and skill_name not in allowlist:
            decisions.append(
                LinkDecision(
                    skill_name,
                    skill_dir,
                    "blocked",
                    audit=audit,
                    detail="critical audit finding; not linked (add to allowlist to override)",
                )
            )
            continue

        if target.is_symlink():
            try:
                if target.resolve() == skill_dir.resolve():
                    decisions.append(LinkDecision(skill_name, skill_dir, "unchanged", audit=audit))
                    continue
            except OSError:
                pass

        if target.exists() or target.is_symlink():
            backup_path(target)
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()

        dst_root.mkdir(parents=True, exist_ok=True)
        target.symlink_to(skill_dir)
        action: LinkAction = "linked_with_warning" if audit is not None and audit.severity == "warning" else "linked"
        decisions.append(LinkDecision(skill_name, skill_dir, action, audit=audit))
    return decisions


def link_all_skill_roots(
    config: Config,
    repo: Path,
    dst_root: Path,
    allowlist: frozenset[str] = frozenset(),
    engine: str | None = None,
) -> list[LinkDecision]:
    decisions: list[LinkDecision] = []
    for src_root in collect_runtime_skill_roots(config, repo):
        decisions.extend(link_skill_root(config, src_root, dst_root, allowlist=allowlist, engine=engine))
    return decisions


# --- discovery/listing (validate/list/audit reporting) -----------------------------------


@dataclass(frozen=True)
class DiscoveredSkill:
    name: str
    description: str
    canonical_path: Path
    root_kinds: tuple[str, ...]
    meta: SkillMeta
    audit: AuditResult
    engines: tuple[str, ...] = ALL_ENGINES


def discover_skills(config: Config, repo: Path | None = None) -> list[DiscoveredSkill]:
    roots: list[tuple[str, Path]] = [
        ("tools-global", config.tools_root / "skills"),
        ("tools-approved", config.tools_root / "skills_approved"),
        ("tools-custom", config.tools_root / "skills_custom"),
        ("tools-quarantine", config.quarantine_root),
        ("user-agents", config.account_home / ".agents" / "skills"),
    ]
    if repo is not None:
        roots.append(("repo-local", Path(repo) / ".agents" / "skills"))

    meta_by_path: dict[str, tuple[SkillMeta, AuditResult, Path]] = {}
    root_kinds_by_path: dict[str, set[str]] = {}
    for kind, root in roots:
        if not root.is_dir():
            continue
        for skill_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.is_file():
                continue
            canonical = skill_md.resolve()
            key = str(canonical)
            root_kinds_by_path.setdefault(key, set()).add(kind)
            if key not in meta_by_path:
                meta = parse_skill_frontmatter(skill_md)
                audit = audit_skill_cached(config, skill_md)
                meta_by_path[key] = (meta, audit, canonical)

    results = [
        DiscoveredSkill(
            name=meta.name,
            description=meta.description,
            canonical_path=canonical,
            root_kinds=tuple(sorted(root_kinds_by_path[key])),
            meta=meta,
            audit=audit,
            engines=skill_engines(canonical.parent),
        )
        for key, (meta, audit, canonical) in meta_by_path.items()
    ]
    return sorted(results, key=lambda s: (s.name.lower(), str(s.canonical_path)))


# --- CLI commands --------------------------------------------------------------------


def command_skills_list(args) -> int:
    cfg = load_config(getattr(args, "config", None))
    repo = repo_root(Path(args.repo)) if getattr(args, "repo", None) else None
    for skill in discover_skills(cfg, repo=repo):
        roots = ",".join(skill.root_kinds) or "unknown"
        engines = ",".join(skill.engines)
        print(
            f"{skill.name} :: {skill.description} :: {skill.canonical_path} :: roots={roots}"
            f" :: engines={engines} :: audit={skill.audit.severity}"
        )
    return 0


def command_skills_audit(args) -> int:
    cfg = load_config(getattr(args, "config", None))
    repo = repo_root(Path(args.repo)) if getattr(args, "repo", None) else None
    any_critical = False
    for skill in discover_skills(cfg, repo=repo):
        if not skill.audit.findings:
            continue
        print(f"{skill.name} :: {skill.canonical_path}")
        for finding in skill.audit.findings:
            print(f"  {finding.severity}: line {finding.line_no}: {finding.rule_name}: {finding.message}: {finding.line_text}")
            if finding.severity == "critical":
                any_critical = True
    return 1 if any_critical else 0


def command_validate_skills(args) -> int:
    cfg = load_config(getattr(args, "config", None))
    repo = repo_root(Path(args.repo)) if getattr(args, "repo", None) else None
    discovered = discover_skills(cfg, repo=repo)
    invalid = [skill for skill in discovered if not skill.meta.is_valid]
    for skill in discovered:
        status = "valid" if skill.meta.is_valid else f"invalid ({', '.join(skill.meta.reasons)})"
        print(f"{skill.name} :: {status}")
    return 1 if invalid else 0


__all__ = [
    "ALL_ENGINES",
    "AUDIT_RULES",
    "AuditFinding",
    "AuditResult",
    "DiscoveredSkill",
    "LinkAction",
    "LinkDecision",
    "SkillMeta",
    "audit_skill",
    "audit_skill_cached",
    "audit_skill_dir",
    "audit_skill_text",
    "skill_engines",
    "collect_runtime_skill_roots",
    "command_skills_audit",
    "command_skills_list",
    "command_validate_skills",
    "discover_skills",
    "link_all_skill_roots",
    "link_skill_root",
    "parse_skill_frontmatter",
]
