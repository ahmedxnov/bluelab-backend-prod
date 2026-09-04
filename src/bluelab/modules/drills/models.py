"""SQLAlchemy models for the tables this module owns (data/01 §4).

Column conventions and the scope-column standard come from
`bluelab.platform.db.base`; nothing here restates them.

Also holds `authoring_option` (data/01 §1, class P0) — the provided
challenge/hidden-motive library (FR-DRL-002). Custom entries are free text on the
drill's concealed set, not rows here.

**The concealed set is physically split.** `drill_concealed` is its own table so a
row-level policy makes leakage structurally hard rather than
projection-dependent. Rubric *weights* — the third concealed element — stay on
`rubric_dimension` and are concealed by the Review module's projection instead;
that split is deliberate and recorded in ADR-0031 §4.

**Test calls store nothing** (FR-DRL-012): no attempt, no transcript, no capture.
There is no table here for them because "leaves no trace in any statistic"
(AC-DRL-005) means exactly that.
"""

from __future__ import annotations

from datetime import date, datetime
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
from bluelab.platform.db.types import (
    SMALLINT_TYPE,
    SNAPSHOT_TYPE,
    WEIGHT_TYPE,
    weight_check,
)

AUTHORING_OPTION_KINDS = ("challenge", "hidden_motive")
DRILL_STATUSES = ("draft", "published", "archived")
CALL_TYPES = ("discovery", "post_proposal", "renewal", "upsell")
LEAD_TYPES = ("inbound_quote", "referral", "cold_outreach")


class AuthoringOption(UUIDPrimaryKey, Timestamped, Base):
    """The provided challenge / hidden-motive library (P0, no org)."""

    __tablename__ = "authoring_option"

    kind: Mapped[str] = mapped_column(nullable=False)
    label: Mapped[str] = mapped_column(nullable=False)
    active: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))

    __table_args__ = (enum_check("kind", AUTHORING_OPTION_KINDS),)


class Drill(UUIDPrimaryKey, Mutable, Base):
    """A drill: the unit of practice and of assessment.

    Published content is **immutable** — changing content means a new drill
    (FR-DRL-015), enforced by `trg_drill_freeze`. Comparability depends on it:
    two candidates on one position must face identical drills.
    """

    __tablename__ = "drill"

    org_id: Mapped[UUID] = mapped_column(ForeignKey("org.id"), nullable=False, index=True)
    team_id: Mapped[UUID] = mapped_column(ForeignKey("account.id"), nullable=False, index=True)
    author_account_id: Mapped[UUID] = mapped_column(ForeignKey("account.id"), nullable=False)

    self_authored: Mapped[bool] = mapped_column(nullable=False)
    """A rep-private drill (FR-TRP-009): never assigned, never shared, excluded
    from every team analytic, and **invisible even to the manager**
    (AC-TRP-004)."""

    status: Mapped[str] = mapped_column(nullable=False, server_default=text("'draft'"))
    call_type: Mapped[str] = mapped_column(nullable=False)
    lead_type: Mapped[str | None] = mapped_column(nullable=True)
    language: Mapped[str] = mapped_column(nullable=False, server_default=text("'ar-EG'"))
    label: Mapped[str | None] = mapped_column(nullable=True)

    scenario: Mapped[dict[str, Any] | None] = mapped_column(SNAPSHOT_TYPE, nullable=True)
    answer_key: Mapped[dict[str, Any] | None] = mapped_column(SNAPSHOT_TYPE, nullable=True)
    """Frozen at publish (ADR-0032, FR-DRL-014).

    Read whole, never queried into — hence no GIN index (data/00 §7). Each carries
    a `"v"` and every historical version must stay readable forever
    (`platform.db.types.SnapshotReader`).
    """

    content_hash: Mapped[str | None] = mapped_column(nullable=True)
    """sha256 over the frozen content — the NFR-004 re-grade evidence anchor."""

    published_at: Mapped[datetime | None] = mapped_column(nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        enum_check("status", DRILL_STATUSES),
        enum_check("call_type", CALL_TYPES),
        enum_check("lead_type", LEAD_TYPES),
        CheckConstraint("language = 'ar-EG'", name="language_v1"),
        # Lead type exists on Discovery and only on Discovery (FR-DRL-001).
        CheckConstraint(
            "(call_type = 'discovery') = (lead_type is not null)", name="lead_type_on_discovery"
        ),
        # Publish-gate backstop: a published drill cannot lack its frozen content.
        CheckConstraint(
            "(status not in ('published','archived')) or "
            "(scenario is not null and answer_key is not null "
            "and label is not null and published_at is not null)",
            name="published_is_complete",
        ),
        scope_key("org_id"),
        scope_key("org_id", "self_authored"),  # lets children FK-carry the privacy flag
        scope_key("org_id", "team_id"),
        Index("idx_drill_team_status", "team_id", "status"),
        Index(
            "idx_drill_author_self",
            "author_account_id",
            postgresql_where=text("self_authored"),
        ),
    )


class DrillConcealed(Base):
    """The concealed narrative set: challenges and hidden motives (FR-SCR-017).

    Split into its own table so that concealment survives a careless join. The
    runtime bundle handed to the call plane declares **no field** for any of this
    (ADR-0071 rule 5), so a leak would have to be deliberate in two places.

    Never revealed to a non-author — in either product, even after the attempt
    ends. Authorship lifts concealment; nothing else does.
    """

    __tablename__ = "drill_concealed"

    drill_id: Mapped[UUID] = mapped_column(primary_key=True)
    org_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    team_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    challenges: Mapped[list[Any]] = mapped_column(
        SNAPSHOT_TYPE, nullable=False, server_default=text("'[]'")
    )
    hidden_motives: Mapped[list[Any]] = mapped_column(
        SNAPSHOT_TYPE, nullable=False, server_default=text("'[]'")
    )

    __table_args__ = (
        scoped_fk(
            columns=("drill_id", "org_id", "team_id"),
            parent="drill",
            parent_columns=("id", "org_id", "team_id"),
            ondelete="CASCADE",
        ),
    )


class RubricDimension(UUIDPrimaryKey, Base):
    """One rubric dimension (FR-DRL-008/009).

    Relational rather than JSONB because scorecards and moments join to it.
    Weights are author-tunable pre-publish; names and rationales are
    generation-fixed. Sum-to-100 is validated in T-5, not here — a row constraint
    cannot see its siblings.
    """

    __tablename__ = "rubric_dimension"

    org_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    team_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    drill_id: Mapped[UUID] = mapped_column(nullable=False)
    ord: Mapped[int] = mapped_column(nullable=False)
    name: Mapped[str] = mapped_column(nullable=False)
    weight: Mapped[int] = mapped_column(WEIGHT_TYPE, nullable=False)
    rationale: Mapped[str] = mapped_column(nullable=False)

    __table_args__ = (
        weight_check("weight"),
        scoped_fk(
            columns=("drill_id", "org_id", "team_id"),
            parent="drill",
            parent_columns=("id", "org_id", "team_id"),
            ondelete="CASCADE",
        ),
        # Deferrable: regeneration replaces the rubric wholesale, so ordinals
        # legitimately collide mid-transaction.
        UniqueConstraint("drill_id", "ord", deferrable=True, initially="DEFERRED"),
    )


class Assignment(UUIDPrimaryKey, Mutable, Base):
    """A drill assigned to reps (FR-TRM-011/012/013).

    **At most one per drill** — re-assignment updates, never duplicates, which is
    what makes `PUT /drills/{id}/assignment` full-replace rather than additive.
    Assignment deliberately sends no email (specs/00 §6 is closed).
    """

    __tablename__ = "assignment"

    org_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    team_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    drill_id: Mapped[UUID] = mapped_column(nullable=False, unique=True)

    due_date: Mapped[date] = mapped_column(nullable=False)
    """An org-local calendar date: "due" means the end of that day in
    `org.timezone`, resolved by `platform.clock.due_date_deadline` — never UTC
    midnight."""

    attempts_allowed: Mapped[int] = mapped_column(SMALLINT_TYPE, nullable=False)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("account.id"), nullable=False)

    __table_args__ = (
        CheckConstraint("attempts_allowed > 0", name="attempts_allowed_positive"),
        scoped_fk(
            columns=("drill_id", "org_id", "team_id"),
            parent="drill",
            parent_columns=("id", "org_id", "team_id"),
            ondelete="CASCADE",
        ),
        scope_key("org_id", "team_id"),
    )


class AssignmentRecipient(Base):
    """Per-rep allowance state (FR-TRP-013).

    `attempts_used` is the strongly-consistent consumption counter: incremented
    under the T-1 admission CAS, **decremented on interruption** so a failed
    attempt is void and free (FR-LIV-015), and reset by re-assignment's fresh
    allowance. The CHECK is the backstop behind the CAS, not the enforcement.
    """

    __tablename__ = "assignment_recipient"

    assignment_id: Mapped[UUID] = mapped_column(primary_key=True)
    rep_account_id: Mapped[UUID] = mapped_column(
        ForeignKey("account.id"), primary_key=True
    )
    org_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    team_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    attempts_used: Mapped[int] = mapped_column(
        SMALLINT_TYPE, nullable=False, server_default=text("0")
    )
    granted_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("attempts_used >= 0", name="attempts_used_non_negative"),
        scoped_fk(
            columns=("assignment_id", "org_id", "team_id"),
            parent="assignment",
            parent_columns=("id", "org_id", "team_id"),
            ondelete="CASCADE",
        ),
        Index("idx_ar_rep", "rep_account_id"),
    )
