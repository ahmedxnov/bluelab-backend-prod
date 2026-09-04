"""SQLAlchemy models for the tables this module owns (data/01 §5).

Column conventions and the scope-column standard come from
`bluelab.platform.db.base`; nothing here restates them.

These are the concealment-bearing tables, which is why `bluelab.calls` and
`bluelab.work` reach them through `attempts.py` rather than importing this module
(ADR-0002 boundary rule). Statistics and banding are **derived, never stored** —
the canonical views live in `sql/views/` (ADR-0034).

Two rules the columns encode rather than merely document:

* **`attempt.self_authored` rides the drill FK.** It drives both the
  manager-visibility policy (AC-TRP-004) and the counted pool (FR-TRP-010)
  without a join inside RLS — and because the composite FK carries it, a copy
  that disagrees with the drill is unrepresentable.
* **Candidates get no scorecard, transcript, or moment access at all**
  (FR-SCR-018, AC-CND-003). That is a policy absence rather than a projection
  filter, so there is no row to leak through.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, ForeignKeyConstraint, Index, text
from sqlalchemy.orm import Mapped, mapped_column

from bluelab.platform.db.base import (
    Base,
    UUIDPrimaryKey,
    enum_check,
    scope_key,
    scoped_fk,
)
from bluelab.platform.db.types import SCORE_TYPE, SNAPSHOT_TYPE, score_check

ATTEMPT_STATUSES = ("in_progress", "completed", "grading_pending", "graded", "interrupted")
RECORDING_STATUSES = ("none", "pending", "available", "unavailable", "erased")
SPEAKERS = ("participant", "buyer")
SEVERITIES = ("green", "amber", "red")


class Attempt(UUIDPrimaryKey, Base):
    """One execution of a drill by one participant (FR-SCR-014).

    Exactly one participant reference — a rep or a candidate, never both.
    Interruption leaves the row at `status='interrupted'` and stores no capture:
    the attempt is void and free (FR-LIV-015).
    """

    __tablename__ = "attempt"

    org_id: Mapped[UUID] = mapped_column(ForeignKey("org.id"), nullable=False, index=True)
    team_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    drill_id: Mapped[UUID] = mapped_column(nullable=False)

    rep_account_id: Mapped[UUID | None] = mapped_column(nullable=True)
    candidate_id: Mapped[UUID | None] = mapped_column(nullable=True)
    assessment_stage_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("assessment_stage.id"), nullable=True
    )

    self_authored: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    status: Mapped[str] = mapped_column(nullable=False, server_default=text("'in_progress'"))
    restart: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))

    started_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    ended_at: Mapped[datetime | None] = mapped_column(nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(nullable=True)

    recording_object_key: Mapped[str | None] = mapped_column(nullable=True)
    recording_status: Mapped[str] = mapped_column(nullable=False, server_default=text("'none'"))
    """The object store must be able to be independently unavailable without
    breaking a review — hence a status rather than an assumed-present key
    (FR-SCR-013)."""

    __table_args__ = (
        enum_check("status", ATTEMPT_STATUSES),
        enum_check("recording_status", RECORDING_STATUSES),
        CheckConstraint(
            "num_nonnulls(rep_account_id, candidate_id) = 1", name="exactly_one_participant"
        ),
        CheckConstraint(
            "(candidate_id is null) = (assessment_stage_id is null)", name="candidate_has_stage"
        ),
        CheckConstraint("ended_at is null or ended_at >= started_at", name="ends_after_start"),
        CheckConstraint("duration_seconds is null or duration_seconds >= 0", name="duration_non_negative"),
        # Team-free on purpose: an assessment legitimately spans teams after a
        # position transfer (data/00 §3). self_authored rides the FK so the
        # policy-bearing copy cannot drift from the drill.
        scoped_fk(
            columns=("drill_id", "org_id", "self_authored"),
            parent="drill",
            parent_columns=("id", "org_id", "self_authored"),
        ),
        # History follows the person: ops reassigning a rep to another manager
        # cascades their attempts into the new team's views (FR-IDA-009).
        scoped_fk(
            columns=("rep_account_id", "org_id", "team_id"),
            parent="account",
            parent_columns=("id", "org_id", "team_id"),
            on_update="CASCADE",
        ),
        scoped_fk(
            columns=("candidate_id", "org_id", "team_id"),
            parent="candidate",
            parent_columns=("id", "org_id", "team_id"),
            on_update="CASCADE",
        ),
        scope_key("org_id", "team_id"),
        # One attempt per drill per candidate (FR-HIR-008). T-1's serialised
        # order/restart check is the enforcement point; this is the corruption
        # backstop behind it.
        Index(
            "uq_attempt_stage_completed",
            "candidate_id",
            "assessment_stage_id",
            unique=True,
            postgresql_where=text(
                "candidate_id is not null "
                "and status in ('completed','grading_pending','graded')"
            ),
        ),
        Index(
            "idx_attempt_rep_time",
            "rep_account_id",
            text("started_at desc"),
            postgresql_where=text("rep_account_id is not null"),
        ),
        Index("idx_attempt_team_time", "team_id", text("started_at desc")),
        Index("idx_attempt_drill_rep", "drill_id", "rep_account_id"),
        Index(
            "idx_attempt_candidate",
            "candidate_id",
            "assessment_stage_id",
            postgresql_where=text("candidate_id is not null"),
        ),
    )


class TranscriptEntry(Base):
    """The verbatim timestamped capture (FR-LIV-007/010).

    Bulk-inserted **once**, inside T-2, and **before** the status flip — because
    `trg_scorecard_freeze` rejects any insert after the attempt leaves
    `in_progress`. That ordering is what makes the transcript crossing the
    call-plane seam exactly once atomically safe (ADR-0071 rule 3).

    Natural key — the one exception to UUID PKs (ADR-0030) — because access is
    always a per-attempt ordered read and `seq` is that order.
    """

    __tablename__ = "transcript_entry"

    attempt_id: Mapped[UUID] = mapped_column(primary_key=True)
    seq: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    team_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    speaker: Mapped[str] = mapped_column(nullable=False)
    at_ms: Mapped[int] = mapped_column(nullable=False)
    text_: Mapped[str] = mapped_column("text", nullable=False)
    """Verbatim Egyptian Arabic. Never translated, summarised, or truncated
    (FR-SCR-008)."""

    demeanor_label: Mapped[str | None] = mapped_column(nullable=True)
    """Text-derived, per-utterance, produced live during the call (ADR-0019).
    Participant rows only. The signal stays **off** until its demographic-bias
    validation passes (SEC-031)."""

    __table_args__ = (
        enum_check("speaker", SPEAKERS),
        CheckConstraint("at_ms >= 0", name="at_ms_non_negative"),
        CheckConstraint(
            "speaker = 'participant' or demeanor_label is null", name="demeanor_participant_only"
        ),
        scoped_fk(
            columns=("attempt_id", "org_id", "team_id"),
            parent="attempt",
            parent_columns=("id", "org_id", "team_id"),
            on_update="CASCADE",
            ondelete="CASCADE",
        ),
    )
    # No secondary index beyond the PK: all access is a per-attempt ordered read
    # (data/02 §4, deliberately absent).


class Scorecard(UUIDPrimaryKey, Base):
    """Graded exactly once (FR-SCR-003).

    **The unique `attempt_id` IS the invariant.** T-3 inserts
    `ON CONFLICT DO NOTHING`, so a retried grading job is a silent no-op rather
    than a second opinion. Rows are insert-only under `trg_scorecard_freeze`;
    only the erasure procedure may null the text fields (ADR-0033).
    """

    __tablename__ = "scorecard"

    org_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    team_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    attempt_id: Mapped[UUID] = mapped_column(nullable=False, unique=True)

    overall_score: Mapped[Decimal] = mapped_column(SCORE_TYPE, nullable=False)
    """The weight-weighted mean, computed **arithmetically** by the grading worker
    (FR-SCR-004). The model never emits it — a model asked for an average will
    occasionally produce one that does not match its own dimension scores
    (ADR-0018)."""

    takeaway: Mapped[str | None] = mapped_column(nullable=True)
    grading_meta: Mapped[dict[str, Any]] = mapped_column(
        SNAPSHOT_TYPE, nullable=False, server_default=text("'{}'")
    )
    """Model ids, versions, content hash — **content-free** (R-16, NFR-004
    evidence)."""

    graded_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (
        score_check("overall_score"),
        scoped_fk(
            columns=("attempt_id", "org_id", "team_id"),
            parent="attempt",
            parent_columns=("id", "org_id", "team_id"),
            on_update="CASCADE",
        ),
        scope_key("org_id", "team_id"),
    )


class DimensionScore(Base):
    """One dimension's score and note (FR-SCR-004)."""

    __tablename__ = "dimension_score"

    scorecard_id: Mapped[UUID] = mapped_column(primary_key=True)
    rubric_dimension_id: Mapped[UUID] = mapped_column(
        ForeignKey("rubric_dimension.id"), primary_key=True
    )
    org_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    team_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    score: Mapped[Decimal] = mapped_column(SCORE_TYPE, nullable=False)
    note: Mapped[str | None] = mapped_column(nullable=True)

    __table_args__ = (
        score_check("score"),
        scoped_fk(
            columns=("scorecard_id", "org_id", "team_id"),
            parent="scorecard",
            parent_columns=("id", "org_id", "team_id"),
            on_update="CASCADE",
            ondelete="CASCADE",
        ),
    )


class Moment(UUIDPrimaryKey, Base):
    """Evidence-anchored coaching moments (FR-SCR-007).

    Each ties a rubric dimension to a transcript position, so "Play at 1:24" lands
    on the utterance the note is about. Amber and red carry the quote,
    try-instead, and why-it-matters trio; green may be a one-liner.

    Erasure **deletes** moment rows outright rather than nulling them — a moment
    without its quote is not evidence of anything (ADR-0033).
    """

    __tablename__ = "moment"

    org_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    team_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    scorecard_id: Mapped[UUID] = mapped_column(nullable=False)
    attempt_id: Mapped[UUID] = mapped_column(nullable=False)
    transcript_seq: Mapped[int | None] = mapped_column(nullable=True)
    at_ms: Mapped[int] = mapped_column(nullable=False)
    severity: Mapped[str] = mapped_column(nullable=False)
    rubric_dimension_id: Mapped[UUID] = mapped_column(
        ForeignKey("rubric_dimension.id"), nullable=False
    )
    quote: Mapped[str | None] = mapped_column(nullable=True)
    try_instead: Mapped[str | None] = mapped_column(nullable=True)
    why_it_matters: Mapped[str | None] = mapped_column(nullable=True)

    __table_args__ = (
        enum_check("severity", SEVERITIES),
        CheckConstraint("at_ms >= 0", name="at_ms_non_negative"),
        CheckConstraint("severity = 'green' or quote is not null", name="graded_moment_has_quote"),
        scoped_fk(
            columns=("scorecard_id", "org_id", "team_id"),
            parent="scorecard",
            parent_columns=("id", "org_id", "team_id"),
            on_update="CASCADE",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["attempt_id", "transcript_seq"],
            ["transcript_entry.attempt_id", "transcript_entry.seq"],
        ),
        Index("idx_moment_scorecard", "scorecard_id", "severity"),
    )
