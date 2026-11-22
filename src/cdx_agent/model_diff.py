"""cdx-agent --model-diff A.xml B.xml | --model-diff --lint X.xml (spec 2026-11-20 N1 backing).

Compiles MuJoCo models and compares the COMPILED values (what a run really uses), not the XML text:
  opt (timestep, integrator, cone, impratio), actuators (forcerange, ctrlrange, gear), joints (range, damping, armature,
  frictionloss), geoms (condim, friction, solref, solimp, margin, gap), bodies (mass, inertia, quat), mesh vertex counts,
  keyframes.
--lint: actuators without forcelimited forcerange, hinge/slide joints without a range, non-world bodies with zero mass,
inertia that breaks the triangle inequality.

Needs the `mujoco` package. When this Python lacks it, the module re-runs itself under the project Python named in
`<repo>/.cdx/project.json` ("python"), if any.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Any, Optional, Sequence

TOL = 1e-9


def _mujoco():
    import mujoco  # noqa: PLC0415  (optional dependency)
    return mujoco


def _project_python(start: str) -> Optional[str]:
    try:
        common = subprocess.run(["git", "-C", start, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not common:
        return None
    f = os.path.join(os.path.dirname(common), ".cdx", "project.json")
    try:
        py = json.load(open(f, encoding="utf-8")).get("python")
    except (OSError, ValueError):
        return None
    return py if py and os.path.realpath(py) != os.path.realpath(sys.executable) else None


def _name(m, objtype, i: int, prefix: str) -> str:
    mj = _mujoco()
    n = mj.mj_id2name(m, objtype, i)
    return n if n else f"{prefix}#{i}"


def _vals(arr, i: int) -> list:
    row = arr[i]
    try:
        return [float(x) for x in row]
    except TypeError:
        return [float(row)]


def summarize(m) -> dict[str, dict[str, Any]]:
    """{section: {item: {field: value}}} of compiled model values."""
    mj = _mujoco()
    o = mj.mjtObj
    out: dict[str, dict[str, Any]] = {"opt": {"opt": {
        "timestep": float(m.opt.timestep), "integrator": int(m.opt.integrator), "cone": int(m.opt.cone),
        "impratio": float(m.opt.impratio), "gravity": [float(x) for x in m.opt.gravity]}}}
    sec = out.setdefault("actuator", {})
    for i in range(m.nu):
        sec[_name(m, o.mjOBJ_ACTUATOR, i, "actuator")] = {
            "forcelimited": int(m.actuator_forcelimited[i]), "forcerange": _vals(m.actuator_forcerange, i),
            "ctrlrange": _vals(m.actuator_ctrlrange, i), "gear0": float(m.actuator_gear[i][0])}
    sec = out.setdefault("joint", {})
    for i in range(m.njnt):
        dof = int(m.jnt_dofadr[i])
        sec[_name(m, o.mjOBJ_JOINT, i, "joint")] = {
            "type": int(m.jnt_type[i]), "limited": int(m.jnt_limited[i]), "range": _vals(m.jnt_range, i),
            "damping": float(m.dof_damping[dof]), "armature": float(m.dof_armature[dof]),
            "frictionloss": float(m.dof_frictionloss[dof])}
    sec = out.setdefault("geom", {})
    for i in range(m.ngeom):
        sec[_name(m, o.mjOBJ_GEOM, i, "geom")] = {
            "condim": int(m.geom_condim[i]), "friction": _vals(m.geom_friction, i), "solref": _vals(m.geom_solref, i),
            "solimp": _vals(m.geom_solimp, i), "margin": float(m.geom_margin[i]), "gap": float(m.geom_gap[i])}
    sec = out.setdefault("body", {})
    for i in range(1, m.nbody):
        sec[_name(m, o.mjOBJ_BODY, i, "body")] = {
            "mass": float(m.body_mass[i]), "inertia": _vals(m.body_inertia, i), "quat": _vals(m.body_quat, i)}
    sec = out.setdefault("mesh", {})
    for i in range(m.nmesh):
        sec[_name(m, o.mjOBJ_MESH, i, "mesh")] = {"vertices": int(m.mesh_vertnum[i])}
    sec = out.setdefault("key", {})
    for i in range(m.nkey):
        sec[_name(m, o.mjOBJ_KEY, i, "key")] = {"qpos": _vals(m.key_qpos, i)}
    return out


def load(path: str):
    return _mujoco().MjModel.from_xml_path(path)


def _differs(a, b) -> bool:
    if isinstance(a, list) and isinstance(b, list):
        return len(a) != len(b) or any(_differs(x, y) for x, y in zip(a, b))
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) > TOL * max(1.0, abs(float(a)), abs(float(b)))
        except (TypeError, ValueError):
            return a != b
    return a != b


def diff(sa: dict, sb: dict) -> list[tuple[str, str, str, Any, Any]]:
    rows = []
    for section in sorted(set(sa) | set(sb)):
        A, B = sa.get(section, {}), sb.get(section, {})
        for item in sorted(set(A) | set(B)):
            if item not in A:
                rows.append((section, item, "<added>", None, B[item]))
                continue
            if item not in B:
                rows.append((section, item, "<removed>", A[item], None))
                continue
            for field in sorted(set(A[item]) | set(B[item])):
                x, y = A[item].get(field), B[item].get(field)
                if _differs(x, y):
                    rows.append((section, item, field, x, y))
    return rows


def lint(s: dict) -> list[str]:
    out = []
    for name, a in s.get("actuator", {}).items():
        if not a["forcelimited"]:
            out.append(f"actuator {name}: no force limit (forcerange not enforced)")
    for name, j in s.get("joint", {}).items():
        if j["type"] in (2, 3) and not j["limited"]:   # mjJNT_SLIDE=2, mjJNT_HINGE=3
            out.append(f"joint {name}: hinge/slide without a range")
    for name, b in s.get("body", {}).items():
        if b["mass"] <= 0:
            out.append(f"body {name}: zero mass")
        i1, i2, i3 = sorted(b["inertia"])
        if b["mass"] > 0 and i1 + i2 < i3 * (1 - 1e-9):
            out.append(f"body {name}: inertia {b['inertia']} breaks the triangle inequality")
    return out


def _fmt(v) -> str:
    if isinstance(v, list):
        return "[" + ", ".join(f"{x:g}" if isinstance(x, float) else str(x) for x in v) + "]"
    if isinstance(v, float):
        return f"{v:g}"
    return json.dumps(v) if isinstance(v, dict) else str(v)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="cdx-agent --model-diff", description=__doc__.split("\n")[0])
    ap.add_argument("models", nargs="+", help="A.xml B.xml (diff) or X.xml with --lint")
    ap.add_argument("--lint", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        _mujoco()
    except ImportError:
        py = _project_python(os.getcwd())
        if py:
            return subprocess.call([py, "-m", "cdx_agent.model_diff", *(argv if argv is not None else sys.argv[1:])],
                                   env={**os.environ, "PYTHONPATH": os.path.dirname(os.path.dirname(__file__))})
        print("model-diff: the mujoco package is not importable here and .cdx/project.json names no python",
              file=sys.stderr)
        return 2
    if a.lint:
        rc = 0
        for p in a.models:
            issues = lint(summarize(load(p)))
            for line in issues:
                print(f"{p}: {line}")
            rc |= 1 if issues else 0
            if not issues:
                print(f"{p}: clean")
        return rc
    if len(a.models) != 2:
        ap.error("diff needs exactly two models")
    rows = diff(summarize(load(a.models[0])), summarize(load(a.models[1])))
    if a.json:
        print(json.dumps([dict(zip(("section", "item", "field", "a", "b"), r)) for r in rows], indent=1))
    else:
        for section, item, field, x, y in rows:
            print(f"{section:9s} {item:28s} {field:12s} {_fmt(x)} -> {_fmt(y)}")
        print(f"{len(rows)} compiled difference(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
