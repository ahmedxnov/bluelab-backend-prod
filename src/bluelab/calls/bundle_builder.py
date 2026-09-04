"""Builds the runtime bundle from the frozen drill (api/02 §1.1).

**Security-relevant component.** This is the one place a concealment leak could
originate, so it sits in the invariant-path quality band (quality/01 §4,
ADR-0071 negative consequence 3): branch coverage plus mutation testing plus an
explicit adversarial case.

The bundle model declares no field for product facts, the rubric, the answer key,
or any element of `drill_concealed`, and forbids unknown keys — so a smuggled key
fails to parse rather than reaching prompt assembly. The model itself lives in
`bluelab_runtime_bundle`, version-locked with the call plane.
"""
