"""SQLAlchemy models for the tables this module owns (data/01 §6).

Column conventions and the scope-column standard come from
`bluelab.platform.db.base`; nothing here restates them.

Also holds `badge` (data/01 §1, class P0) — the system-defined badge catalogue
whose rules are seed configuration evaluated by this module (FR-TRP-005).

**This module stores almost nothing.** Ratings, tiers, gap analysis, rosters and
leaderboards are all derived on read from `sql/views/` (ADR-0034) — no aggregate
tables and no materialized views, which is how FR-TRP-002's identical-numbers
guarantee holds by construction rather than by a refresh job. The only rows here
are the two things that genuinely cannot be derived.

Both are class **P5, owner-private**: private to the rep, invisible even to the
manager. That is the coaching-not-surveillance principle as a database property.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import ForeignKey, Index, text
from sqlalchemy.orm import Mapped, mapped_column

from bluelab.platform.db.base import Base, Timestamped, UUIDPrimaryKey


class Badge(Base):
    """System-defined badges (P0, no org).

    `code` is the primary key rather than a UUID: badges are seed configuration
    referenced by a stable name, and a literal code is what makes the seed
    migration idempotent across environments (data/04 §6).
    """

    __tablename__ = "badge"

    code: Mapped[str] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(nullable=False)
    rule_text: Mapped[str] = mapped_column(nullable=False)
    active: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))


class CoachFeedbackItem(UUIDPrimaryKey, Timestamped, Base):
    """The rep-home coaching feed (FR-TRP-003).

    Rule-based entries derived from scorecard data — **not a model call**.
    Recurring weaknesses are already visible in per-dimension scores across
    attempts, so v1 derives the feed in this module rather than adding a sixth
    job type (architecture/00 §3.3, gate finding F-1).

    Visible only to the rep. The unread count is the one notification badge that
    exists anywhere in v1 (ux/00 §3.1).
    """

    __tablename__ = "coach_feedback_item"

    org_id: Mapped[UUID] = mapped_column(ForeignKey("org.id"), nullable=False, index=True)
    rep_account_id: Mapped[UUID] = mapped_column(ForeignKey("account.id"), nullable=False)
    body: Mapped[str] = mapped_column(nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        Index("idx_feedback_rep", "rep_account_id", text("created_at desc")),
        Index(
            "idx_feedback_unread",
            "rep_account_id",
            postgresql_where=text("read_at is null"),
        ),
    )


class BadgeAward(Base):
    """Earned badges (FR-TRP-005).

    Private to their owner and shown on no other surface — "only you see these".
    No manager policy exists on this table, so a leaderboard cannot accidentally
    join to it.
    """

    __tablename__ = "badge_award"

    badge_code: Mapped[str] = mapped_column(ForeignKey("badge.code"), primary_key=True)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("account.id"), primary_key=True)
    org_id: Mapped[UUID] = mapped_column(ForeignKey("org.id"), nullable=False, index=True)
    earned_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
