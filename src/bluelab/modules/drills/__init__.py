"""Drill Authoring — FR-DRL-* (specs/12-drill-lifecycle.spec.md).

Call-type taxonomy, intent capture, orchestration of AI generation, rubric tuning
and the sum-to-100 gate, draft state, test calls, publish-and-freeze, archive.

Two rules dominate the shape:
  * **Comparability is sacred.** Published drill content is immutable; changing
    content means a new drill (FR-DRL-015, ADR-0007).
  * **Concealment.** The generative inputs and the rubric weights live in
    `drill_concealed`, physically split from `drill` so the participant-safe
    projection cannot leak by accident (data/01 §4, ADR-0031 class P3).
"""
