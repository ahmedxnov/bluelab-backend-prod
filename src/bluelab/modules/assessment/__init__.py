"""Assessment — FR-CND-* (specs/23-candidate-flow.spec.md).

The candidate's journey: token entry, hardware pre-flight that hard-blocks on
failure, the plan, stage progression and its ordering guard, continuity, and the
terminal completion screen. No account, one sitting, no resume.

**Candidates see no evaluation, ever** — no candidate-authenticated endpoint
serializes a score, band, scorecard, review, or report field (FR-CND-007,
FR-SCR-018). That is a property of this module's schemas, checkable in the
OpenAPI document itself, and structural at the database besides: no candidate RLS
policy exists on `rubric_dimension` at all (ADR-0031 §4).
"""
