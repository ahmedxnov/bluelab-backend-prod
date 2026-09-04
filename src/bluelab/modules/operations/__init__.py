"""Operations — the BlueLab-internal surface (ADR-0010).

Provisioning and deactivation (FR-IDA-001 / FR-IDA-010), team change, position
transfer (FR-HIR-018), the ops fault queue raised by FR-SCR-009 / FR-SCR-013, the
append-only audit trail, and the subject-rights procedures (erasure and export,
T-10).

Widest privilege, deliberately narrow: ops holds **no customer scope tuple** —
access is verb-scoped only, and ops's inability to read transcripts, reviews, or
knowledge is a *database* property, not an API courtesy (ADR-0031 class P9).
"""
