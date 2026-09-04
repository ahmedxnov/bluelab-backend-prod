"""SQLAlchemy models for the tables this module owns (data/01 §3).

Column conventions and the scope-column standard come from
`bluelab.platform.db.base`; nothing here restates them.

**Fact sets are how publish stays atomic.** A document has exactly one `live` set
— the published truth — and at most one `draft` awaiting review. Publishing swaps
pointer-free: the draft becomes live and the old live set is deleted (T-4). Two
partial unique indexes make "exactly one live, at most one draft" a database
property rather than a transaction's good behaviour.

Nothing on the participant path reads these tables. A drill freezes its own
answer-key snapshot at publish (FR-KNW-007, ADR-0007), so a later fact change
cannot retroactively alter how an old attempt was graded.
"""

from __future__ import annotations

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

FACT_SET_KINDS = ("live", "draft")
FACT_SOURCES = ("upload", "manual")
UPLOAD_STATUSES = ("received", "extracting", "extracted", "failed")

MAX_UPLOAD_BYTES = 20 * 1024 * 1024


class ProductDocument(UUIDPrimaryKey, Mutable, Base):
    """A manager-published source document (FR-KNW-001)."""

    __tablename__ = "product_document"

    org_id: Mapped[UUID] = mapped_column(ForeignKey("org.id"), nullable=False, index=True)
    team_id: Mapped[UUID] = mapped_column(ForeignKey("account.id"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(nullable=False)

    live_version: Mapped[int] = mapped_column(nullable=False, server_default=text("0"))
    """Optimistic-concurrency counter. T-4's publish carries `based_on_version`
    and loses with `409 stale-review` if this moved underneath it (FR-KNW-011)."""

    __table_args__ = (scope_key("org_id", "team_id"),)
    # The 10-document cap (FR-KNW-001) is an application gate at creation, not a
    # DB constraint — a single row cannot see its siblings (data/00 §7).


class FactSet(UUIDPrimaryKey, Timestamped, Base):
    """A document's facts, in one of two roles: `live` or `draft`."""

    __tablename__ = "fact_set"

    org_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    team_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    document_id: Mapped[UUID] = mapped_column(nullable=False)
    kind: Mapped[str] = mapped_column(nullable=False)
    source: Mapped[str] = mapped_column(nullable=False)

    based_on_version: Mapped[int | None] = mapped_column(nullable=True)
    """Draft only: the `live_version` it was built against — the CAS value T-4
    compares."""

    created_by: Mapped[UUID] = mapped_column(ForeignKey("account.id"), nullable=False)

    __table_args__ = (
        enum_check("kind", FACT_SET_KINDS),
        enum_check("source", FACT_SOURCES),
        CheckConstraint(
            "(kind = 'draft') = (based_on_version is not null)", name="draft_carries_base_version"
        ),
        scoped_fk(
            columns=("document_id", "org_id", "team_id"),
            parent="product_document",
            parent_columns=("id", "org_id", "team_id"),
            ondelete="CASCADE",
        ),
        Index("uq_fact_set_live", "document_id", unique=True, postgresql_where=text("kind = 'live'")),
        Index("uq_fact_set_draft", "document_id", unique=True, postgresql_where=text("kind = 'draft'")),
    )


class ProductFact(UUIDPrimaryKey, Base):
    """One published fact (FR-KNW-002)."""

    __tablename__ = "product_fact"

    org_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    team_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    fact_set_id: Mapped[UUID] = mapped_column(
        ForeignKey("fact_set.id", ondelete="CASCADE"), nullable=False
    )
    ord: Mapped[int] = mapped_column(nullable=False)
    label: Mapped[str] = mapped_column(nullable=False)
    value: Mapped[str] = mapped_column(nullable=False)
    note: Mapped[str | None] = mapped_column(nullable=True)

    prior_fact_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("product_fact.id", ondelete="SET NULL"), nullable=True
    )
    """The diff anchor into the live set (FR-KNW-005).

    *Changed* = linked with a differing value · *added* = unlinked · *removed* =
    a live fact no draft row links to. Set from row identity on the manual path
    and by label match on the upload path. Self-clears when the superseded set
    departs at publish, which is what keeps T-4's reject-and-rebuild honest.
    """

    __table_args__ = (UniqueConstraint("fact_set_id", "ord"),)


class DocumentUpload(UUIDPrimaryKey, Timestamped, Base):
    """A replacement source file (FR-KNW-003/008).

    The file itself lives in the object store; extraction is a work-plane job.
    **Failure leaves the live facts untouched** — nothing reaches review, and the
    retry is a new upload rather than a re-run (FR-KNW-008).
    """

    __tablename__ = "document_upload"

    org_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    team_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    document_id: Mapped[UUID] = mapped_column(nullable=False)
    object_key: Mapped[str] = mapped_column(nullable=False)
    filename: Mapped[str] = mapped_column(nullable=False)
    byte_size: Mapped[int] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(nullable=False, server_default=text("'received'"))
    failure_reason: Mapped[str | None] = mapped_column(nullable=True)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("account.id"), nullable=False)

    __table_args__ = (
        enum_check("status", UPLOAD_STATUSES),
        CheckConstraint(
            f"byte_size between 1 and {MAX_UPLOAD_BYTES}", name="byte_size_range"
        ),
        scoped_fk(
            columns=("document_id", "org_id", "team_id"),
            parent="product_document",
            parent_columns=("id", "org_id", "team_id"),
            ondelete="CASCADE",
        ),
        Index("idx_upload_document", "document_id", text("created_at desc")),
    )
