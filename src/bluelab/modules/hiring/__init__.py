"""Hiring — FR-HIR-* (specs/22-hiring-manager.spec.md).

Positions, assessment composition and freeze, candidate batches and invites, the
pipeline, the report, decisions, and the HR handoff.

Invariants owned here: the assessment freezes on first invite (T-7); a decision
freezes on shortlist membership (T-9); the candidate overall is the *unweighted*
mean of per-drill overalls (FR-HIR-011, V-9); AI output is decision support — the
manager decides.
"""
