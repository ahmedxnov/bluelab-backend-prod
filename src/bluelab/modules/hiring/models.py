"""SQLAlchemy models for the tables this module owns (data/01 §7).

Column conventions and the scope-column standard come from
`bluelab.platform.db.base`; nothing here restates them.

`assessment_stage` lives here rather than in the assessment module: composition
is FR-HIR-004, a manager act. The assessment module reads it on the candidate
path and owns no tables of its own (see `assessment/models.py`).

**A position is owned by its manager** — `team_id` *is* the owner — so a transfer
(FR-HIR-018) is one `UPDATE position SET team_id`, and every composite FK below
carries `ON UPDATE CASCADE` so stages, candidates, tokens, attempts, reports and
shortlists follow atomically. That is the whole mechanism; there is no transfer
procedure walking a tree.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from bluelab.platform.db.base import (
    Base,
    Mutable,
    Timestamped,
    UUIDPrimaryKey,
    enum_check,
    scope_key,
    scoped_fk,
)
from bluelab.platform.db.types import SMALLINT_TYPE, SNAPSHOT_TYPE

REPORT_POLICIES = ("after_finish", "rejected_only", "withhold")
POSITION_STATUSES = ("needs_authoring", "active", "closed")
DECISIONS = ("pending", "approved", "rejected")
PDF_STATUSES = ("none", "pending", "available", "failed", "erased")


class Position(UUIDPrimaryKey, Mutable, Base):
    """A role being hired for (FR-HIR-001/003)."""

    __tablename__ = "position"

    org_id: Mapped[UUID] = mapped_column(ForeignKey("org.id"), nullable=False, index=True)
    team_id: Mapped[UUID] = mapped_column(ForeignKey("account.id"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(nullable=False)
    openings: Mapped[int] = mapped_column(SMALLINT_TYPE, nullable=False)
    notify_on_completion: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    report_policy: Mapped[str] = mapped_column(nullable=False, server_default=text("'rejected_only'"))
    invite_expiry_days: Mapped[int] = mapped_column(SMALLINT_TYPE, nullable=False, server_default=text("7"))
    invite_template: Mapped[str | None] = mapped_column(nullable=True)
    """The editable invite body. **Must carry the desktop-requirement line** —
    owner copy, presence non-negotiable (ux C-2, ADR-0045), asserted in the E-2
    template test."""

    status: Mapped[str] = mapped_column(nullable=False, server_default=text("'needs_authoring'"))

    assessment_frozen_at: Mapped[datetime | None] = mapped_column(nullable=True)
    """Set at the first invite (T-7). Once set, `trg_stage_freeze` rejects every
    stage change — comparability requires that two candidates on one position
    faced the identical assessment (FR-HIR-005)."""

    closed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        enum_check("report_policy", REPORT_POLICIES),
        enum_check("status", POSITION_STATUSES),
        CheckConstraint("openings > 0", name="openings_positive"),
        CheckConstraint("invite_expiry_days > 0", name="invite_expiry_positive"),
        scope_key("org_id", "team_id"),
    )


class AssessmentStage(UUIDPrimaryKey, Base):
    """The ordered drill set candidates face (FR-HIR-004)."""

    __tablename__ = "assessment_stage"

    org_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    team_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    position_id: Mapped[UUID] = mapped_column(nullable=False)
    ord: Mapped[int] = mapped_column(nullable=False)
    drill_id: Mapped[UUID] = mapped_column(nullable=False)

    __table_args__ = (
        scoped_fk(
            columns=("position_id", "org_id", "team_id"),
            parent="position",
            parent_columns=("id", "org_id", "team_id"),
            on_update="CASCADE",
            ondelete="CASCADE",
        ),
        # Team-free: after a transfer the assessment legitimately references
        # another team's frozen drills (data/00 §3, FR-HIR-018).
        scoped_fk(
            columns=("drill_id", "org_id"),
            parent="drill",
            parent_columns=("id", "org_id"),
        ),
        UniqueConstraint("position_id", "ord", deferrable=True, initially="DEFERRED"),
        Index("idx_stage_drill", "drill_id"),
    )


class Candidate(UUIDPrimaryKey, Mutable, Base):
    """One applicant for one position (FR-HIR-006). No account, ever."""

    __tablename__ = "candidate"

    org_id: Mapped[UUID] = mapped_column(ForeignKey("org.id"), nullable=False, index=True)
    team_id: Mapped[UUID] = mapped_column(ForeignKey("account.id"), nullable=False, index=True)
    position_id: Mapped[UUID] = mapped_column(nullable=False)

    name: Mapped[str] = mapped_column(nullable=False)
    email: Mapped[str] = mapped_column(nullable=False)
    phone: Mapped[str | None] = mapped_column(nullable=True)
    linkedin: Mapped[str | None] = mapped_column(nullable=True)
    source: Mapped[str | None] = mapped_column(nullable=True)

    internal_note: Mapped[str | None] = mapped_column(nullable=True)
    """Manager-private. Concealed at projection and **absent from the PDF by
    construction** — the render worker never reads this column (FR-HIR-012,
    api/02 §4)."""

    preflight: Mapped[dict[str, Any] | None] = mapped_column(SNAPSHOT_TYPE, nullable=True)
    """Hardware-check results and timestamp (FR-CND-002)."""

    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    """The terminal marker (FR-CND-008). Also terminates every token at once —
    token termination is derived from this, not stored per token (FR-IDA-013)."""

    decision: Mapped[str] = mapped_column(nullable=False, server_default=text("'pending'"))
    decided_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        enum_check("decision", DECISIONS),
        scoped_fk(
            columns=("position_id", "org_id", "team_id"),
            parent="position",
            parent_columns=("id", "org_id", "team_id"),
            on_update="CASCADE",
        ),
        scope_key("org_id", "team_id"),
        Index("idx_candidate_position", "position_id", "decision"),
    )


class CandidateReport(Base):
    """The manager-facing report artifact (FR-HIR-011/012).

    Stored parts only: the cross-drill takeaway (produced by the report worker via
    C-6) and the PDF's object reference. The overall score and the per-drill cards
    are **derived** (V-9) — storing them would let the report and the reviews it
    summarises drift apart.
    """

    __tablename__ = "candidate_report"

    candidate_id: Mapped[UUID] = mapped_column(primary_key=True)
    org_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    team_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    takeaway: Mapped[str | None] = mapped_column(nullable=True)
    generated_at: Mapped[datetime | None] = mapped_column(nullable=True)
    pdf_object_key: Mapped[str | None] = mapped_column(nullable=True)
    pdf_status: Mapped[str] = mapped_column(nullable=False, server_default=text("'none'"))
    pdf_rendered_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        enum_check("pdf_status", PDF_STATUSES),
        scoped_fk(
            columns=("candidate_id", "org_id", "team_id"),
            parent="candidate",
            parent_columns=("id", "org_id", "team_id"),
            on_update="CASCADE",
            ondelete="CASCADE",
        ),
    )


class HrContact(UUIDPrimaryKey, Timestamped, Base):
    """A manager's saved HR recipients (FR-HIR-014).

    Personal to the manager and **never transferred** with a position
    (FR-HIR-018) — hence `owner_account_id` rather than `team_id`.
    """

    __tablename__ = "hr_contact"

    org_id: Mapped[UUID] = mapped_column(ForeignKey("org.id"), nullable=False, index=True)
    owner_account_id: Mapped[UUID] = mapped_column(ForeignKey("account.id"), nullable=False)
    email: Mapped[str] = mapped_column(nullable=False)
    label: Mapped[str | None] = mapped_column(nullable=True)

    __table_args__ = (
        Index("uq_hr_contact", text("owner_account_id"), text("lower(email)"), unique=True),
    )


class Shortlist(UUIDPrimaryKey, Base):
    """One E-4 shortlist send to HR (FR-HIR-014)."""

    __tablename__ = "shortlist"

    org_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    team_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    position_id: Mapped[UUID] = mapped_column(nullable=False)
    sent_by: Mapped[UUID] = mapped_column(ForeignKey("account.id"), nullable=False)

    recipients: Mapped[list[Any]] = mapped_column(SNAPSHOT_TYPE, nullable=False)
    """The resolved recipient list **as sent** — a snapshot, because the manager's
    saved contacts may change afterwards and the record of who received a
    candidate's report must not."""

    email_body: Mapped[str] = mapped_column(nullable=False)
    sent_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (
        scoped_fk(
            columns=("position_id", "org_id", "team_id"),
            parent="position",
            parent_columns=("id", "org_id", "team_id"),
            on_update="CASCADE",
        ),
        scope_key("org_id", "team_id"),
        Index("idx_shortlist_position", "position_id"),
    )


class ShortlistCandidate(Base):
    """Shortlist membership — and the decision freeze (FR-HIR-013).

    **Membership existence is the freeze.** `trg_decision_freeze` rejects a
    `candidate.decision` change while a row here exists, so once a candidate's
    report has gone to HR the decision that was sent cannot be quietly revised.
    A held-back candidate simply has no row and stays approved-unsent.
    """

    __tablename__ = "shortlist_candidate"

    shortlist_id: Mapped[UUID] = mapped_column(primary_key=True)
    candidate_id: Mapped[UUID] = mapped_column(ForeignKey("candidate.id"), primary_key=True)
    org_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    team_id: Mapped[UUID] = mapped_column(nullable=False, index=True)

    __table_args__ = (
        scoped_fk(
            columns=("shortlist_id", "org_id", "team_id"),
            parent="shortlist",
            parent_columns=("id", "org_id", "team_id"),
            on_update="CASCADE",
            ondelete="CASCADE",
        ),
        Index("idx_shortlist_member", "candidate_id"),
    )
