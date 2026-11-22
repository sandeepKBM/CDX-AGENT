"""Experiment configs: YAML with an ordered ``defaults:`` list, dotted overrides, ``-m`` sweeps and tagged bounds.

    load(path)                       merged dict (defaults in order, then the file's own keys)
    resolve(conf, overrides)         conf with ``a.b=1`` overrides applied (values via yaml.safe_load)
    sweep(overrides)                 ``a=1,2 b=x,y`` -> list of override lists (cartesian product); ``range(a,b[,s])`` expands
    config_hash(conf)                sha256 of canonical JSON
    bounds(conf)                     {dotted key: bound}; ValueError naming the key path for a bad tag / MEASURED gap
    new_assumed(conf, repo)          ASSUMED bounds in neither the values ledger nor <repo>/.cdx/assumed_registry.json
"""
from __future__ import annotations

import copy
import hashlib
import itertools
import json
import os
import re
from typing import Any

import yaml

from cdx_agent import values

TAGS = ("MEASURED", "MODEL", "ASSUMED", "USER")
_BOUND_HINTS = ("tag", "source", "on", "n", "stat")


def _merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load(path: str, _seen: tuple[str, ...] = ()) -> dict[str, Any]:
    path = os.path.abspath(path)
    if path in _seen:
        raise ValueError(f"config defaults cycle at {path}")
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: top level must be a mapping")
    defaults = raw.pop("defaults", None) or []
    out: dict[str, Any] = {}
    for d in defaults:
        out = _merge(out, load(os.path.join(os.path.dirname(path), str(d)), _seen + (path,)))
    return _merge(out, raw)


def parse_override(text: str) -> tuple[str, Any]:
    if "=" not in text:
        raise ValueError(f"override must be key=value: {text!r}")
    key, rhs = text.split("=", 1)
    if not key or any(not p for p in key.split(".")):
        raise ValueError(f"bad override key: {key!r}")
    return key, yaml.safe_load(rhs)


def set_dotted(conf: dict[str, Any], key: str, value: Any) -> None:
    node = conf
    parts = key.split(".")
    for p in parts[:-1]:
        if not isinstance(node.get(p), dict):
            node[p] = {}
        node = node[p]
    node[parts[-1]] = value


def resolve(conf: dict[str, Any], overrides: list[str] | tuple[str, ...] = ()) -> dict[str, Any]:
    out = copy.deepcopy(conf)
    for o in overrides:
        k, v = parse_override(o)
        set_dotted(out, k, v)
    return out


def _split_top(text: str) -> list[str]:
    parts, depth, cur = [], 0, ""
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return parts


def _expand(rhs: str) -> list[str]:
    m = re.fullmatch(r"range\(\s*(-?\d+)\s*,\s*(-?\d+)\s*(?:,\s*(-?\d+)\s*)?\)", rhs.strip())
    if m:
        step = int(m.group(3) or 1)
        if step == 0:
            raise ValueError("range step is 0")
        return [str(i) for i in range(int(m.group(1)), int(m.group(2)), step)]
    return [p.strip() for p in _split_top(rhs)]


def sweep(overrides: list[str] | tuple[str, ...]) -> list[list[str]]:
    axes = []
    for o in overrides:
        if "=" not in o:
            raise ValueError(f"override must be key=value: {o!r}")
        k, rhs = o.split("=", 1)
        axes.append([f"{k}={v}" for v in _expand(rhs)])
    return [list(p) for p in itertools.product(*axes)] if axes else [[]]


def config_hash(conf: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(conf, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _is_bound(d: dict[str, Any]) -> bool:
    return "value" in d and any(h in d for h in _BOUND_HINTS)


def bounds(conf: Any, _prefix: str = "") -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(conf, dict):
        return out
    if _is_bound(conf):
        key = _prefix or "<root>"
        tag = conf.get("tag")
        if tag not in TAGS:
            raise ValueError(f"{key}: bound tag must be one of {'|'.join(TAGS)}, got {tag!r}")
        if tag == "MEASURED":
            src = conf.get("source")
            if not (isinstance(src, str) and "/" in src):
                raise ValueError(f"{key}: MEASURED bound needs a source path")
            for need in ("n", "stat"):
                if need not in conf:
                    raise ValueError(f"{key}: MEASURED bound needs '{need}'")
        out[key] = {"value": conf["value"], "tag": tag, "source": conf.get("source"), "on": conf.get("on")}
        return out
    for k, v in conf.items():
        out.update(bounds(v, f"{_prefix}.{k}" if _prefix else str(k)))
    return out


def new_assumed(conf: dict[str, Any], repo: str, ledger: str | None = None) -> list[str]:
    known = set(values.current(ledger))
    reg = os.path.join(repo, ".cdx", "assumed_registry.json")
    try:
        with open(reg, encoding="utf-8") as fh:
            data = json.load(fh)
        known |= set(data.keys() if isinstance(data, dict) else data)
    except (OSError, ValueError):
        pass
    return sorted(k for k, b in bounds(conf).items() if b["tag"] == "ASSUMED" and k not in known)
