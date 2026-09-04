"""Scoring & Review — FR-SCR-* (specs/14-scoring-and-review.spec.md).

Owns the attempt record, the transcript, the scorecard and its dimension scores
and moments, review composition, the statistics primitives, and — the load-bearing
part — **enforcement of the concealment rule on every rendered surface and every
delivered artifact** (FR-SCR-017).

This module is the *sole* renderer of any scorecard-bearing surface. That is what
makes concealment auditable in one place instead of distributed across eight
surfaces (ADR-0002), and it is why `bluelab.calls` and `bluelab.work` reach the
attempt lifecycle through this module's published interface rather than its
tables.
"""
