# Safety-math verification checklist

You are a fresh verifier. You have the formulas and code of a change, not the
author's conclusions. Re-derive the math yourself; do not trust comments.

1. Units and frames: state the unit and coordinate frame of every term in every formula.
2. Sign and direction of every inequality: which way does it point, and does that match the physics?
3. Which side is conservative: for each bound, is an error on the safe or the unsafe side?
4. Discretisation vs physics substeps: does the control/check step dt match the simulator substep, and what happens between checks?
5. Limits come from the config source (limit_sources), not from literals typed into the code.
6. Guarantee type: what is claimed (hard bound, probabilistic, heuristic) versus what the code actually does.
7. One reference value: compute one concrete case a second way (by hand or independent code) and compare with the code's result.
8. Behaviour at box corners: check the extremes of every input range at once, not only one axis at a time.
9. What the check does not cover: list the states, inputs and failure modes it never sees.

Output: findings only, each with file:line, each marked either
"confirmed (re-computed)" or "suspected". No summary verdict such as "looks safe".
