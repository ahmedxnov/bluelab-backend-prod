"""Training — FR-TRP-* and FR-TRM-* (specs/20-training-rep.spec.md,
specs/21-training-manager.spec.md).

The rep surface (progress, profile, library, history, self-authoring deltas,
coach-feedback feed) and the manager surface (dashboard, rating formulas, deep
dives, drill catalog, assignment, team aggregation rules).

Two visibility rules this module must not soften:
  * **Coaching, not surveillance.** Reps never see peer rankings; badges are
    private. Managers do see team leaderboards.
  * **Self-authored drills are private to their author**, never assigned or
    shared, and excluded from all team analytics — invisible even to the manager
    (AC-TRP-004; ADR-0031 class P5).

All ratings and statistics derive on read from the one canonical SQL derivation
(V-1…V-8, `sql/views/`) — no aggregate tables, no materialized views, which is
how the FR-TRP-002 identical-numbers guarantee holds by construction (ADR-0034).
"""
