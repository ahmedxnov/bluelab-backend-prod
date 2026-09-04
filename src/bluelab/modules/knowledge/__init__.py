"""Knowledge — FR-KNW-* (specs/11-knowledge.spec.md).

Product documents and facts, upload and extraction hand-off, the replacement
review with diff, publish, the per-team answer key, the participant-facing
read-only reference, and the frozen snapshot handed to a drill at publish.

Knowledge is the single answer key: published facts ground the AI evaluator and
render read-only to reps and candidates. Publishing replaces current facts after
a review-with-diff step, guarded by the version CAS of T-4 (FR-KNW-011).
"""
