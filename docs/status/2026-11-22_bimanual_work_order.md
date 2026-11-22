# Work order for the bimanual session (from the cdx-agent spec implementation, 2026-11-22)

Source: spec `/common/users/ss5772/downloads/bimanual/2026-11-20_cdx_agent_design/01_cdx_agent_implementation_spec.md`
sections 4.9, 8.2 (N1, N2). These items edit the bimanual repo, so the bimanual session does them; the cdx-agent side is done.

## Tools now available (deployed)
- `cdx-agent --model-diff A.xml B.xml` and `--model-diff --lint X.xml`: compiled MuJoCo values (opt, actuators, joints,
  geoms, bodies, meshes, keys). Checked 2026-11-22 on `assets_nero/nero_arm.xml` (lint clean) and
  `scenes/scene_yor_cup.xml` vs `scenes/scene_yor_canonical.xml` (43 compiled differences).
- `cdx-agent --run`, `--suite`, `--check docs|report|refs`, `--exp`, `--junk scan`, `--node`, `--verify-log`, `--brief --focus safety-math`.
- Hook: `.cdx/hardware.json` `{"patterns": [regex...], "reason": "..."}` makes a matching Bash command ask the user.

## 1. N1 project skill `mujoco-model-checks` (`.claude/skills/mujoco-model-checks/SKILL.md`, with `paths:`)
Text in spec N1. One fact checked here, so it can go in as a fact (MuJoCo 3.9.0, 2026-11-22):
- MjSpec `body.alt` angles are in DEGREES by default (`spec.compiler.degree == 1`): `alt.euler = [0, 0, pi/4]` compiles to a
  0.79 deg rotation (`body_quat` [0.999977, 0, 0, 0.006854]), not 45 deg; `[0, 0, 45]` gives 45 deg. With
  `spec.compiler.degree = False`, pi/4 gives 45 deg. When `alt.type` is set it overrides `quat`.
  Rule: after setting an orientation, read `model.body_quat` back (spec N1 rule 2).
- Point rule-2/limit checks at `cdx-agent --model-diff` (compiled values) instead of reading XML.

## 2. N2 project skill `controller-change-checks` (spec N2 text, `paths:` on flmpc/contents/checks)

## 3. Repo tests and fixes (spec N1, N2)
- `world_model/v2/nero/tests/test_geomdistance_crosscheck.py`: saved separated mesh/box pairs; record where
  `mj_geomDistance` and GJK (`clearance.py::_dist`) disagree.
- A test that fails when a driver overrides the controller's `step()` output on infeasible steps.
- `clearance.py:3` docstring still says "Uses mujoco.mj_geomDistance" (line 93 says it is not used).
- `AGENTS.md:16` says `CUDA_VISIBLE_DEVICES` picks the render GPU; `docs/status/observations_2026-10-12.md:548` says
  `MUJOCO_EGL_DEVICE_ID`. Correct one of them.

## 4. Rule text (spec 4.9)
- `AGENTS.md` Storage section: replace "deletions only via a `!` line" with the tiers (base rules, 2026-11-20):
  (a) allow-listed junk delete + log, (b) `cdx-agent --rm` (trash, 7 days), (c) protected: user only.

## 5. `.cdx/` setup (spec section 5)
- `.cdx/project.json` (`entry`, `python` = the project env Python, `ledger`, `docs_index`, `result_roots`, `limit_sources`).
  `limit_sources` feeds `--brief --focus safety-math`; `python` lets `--model-diff` run under the project env.
- `.cdx/resources.yaml` (two profiles from `suite11.sh`), `.cdx/test_routes.yaml`, `.cdx/long_suites.txt` tags.
- `.cdx/hardware.json` with the robot/arm/gripper launcher patterns.
