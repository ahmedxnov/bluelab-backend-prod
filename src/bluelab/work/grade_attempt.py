"""Grading (FR-SCR-001…009) under NFR-005: p50 <= 20 s, p95 <= 45 s.

Idempotency is `scorecard.attempt_id` unique — T-3 inserts `ON CONFLICT DO
NOTHING`, so a second grade is a silent no-op. While failing, the attempt reads
as `grading_pending` and the participant sees *preparing*, never an error; on
retry exhaustion it raises an `ops_fault`, and it **never voids the call**
(FR-SCR-009).

Uses the decomposed per-dimension evaluator pass of ADR-0018; consistency is
measured against NFR-004 (+/- 0.5 overall, +/- 1 per dimension) by the L9 variance
study and watched in operation by the re-grade canary.
"""
