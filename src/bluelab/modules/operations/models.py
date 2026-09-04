"""SQLAlchemy models for the tables this module owns (data/01 §9).

Column conventions and the scope-column standard come from
`bluelab.platform.db.base`; nothing here restates them.

**Ops staff authenticate on a separate surface with separate accounts** — never a
flag on a customer account. `ops_account` is a different table from `account`
precisely so there is no privilege-escalation path from one to the other.

Ops access to customer tables is verb-scoped: **no policy grants ops any read of
transcripts, scorecards, moments, recordings, or knowledge** (ADR-0031 class P9).
That inability is a database property, not an API courtesy — which is what lets a
small team operate this product without staff reading customer content.

Every table here is content-free by construction: ids, states, counts, and
reasons. Never a quote, never a transcript, never a name.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ForeignKey, Index, text
from sqlalchemy.orm import Mapped, mapped_column

from bluelab.platform.db.base import Base, Timestamped, UUIDPrimaryKey, enum_check
from bluelab.platform.db.types import SNAPSHOT_TYPE

OPS_STATUSES = ("active", "deactivated")
AUDIT_VERBS = (
    "provision_org",
    "provision_account",
    "deactivate_account",
    "change_team_mapping",
    "transfer_position",
    "resolve_fault",
    "execute_erasure",
    "execute_export",
)
FAULT_KINDS = ("grading_failure", "playback_asset")
FAULT_STATUSES = ("open", "resolved")
ERASURE_STATUSES = ("pending", "executed", "failed")
EXPORT_STATUSES = ("pending", "ready", "delivered", "failed")
SUBJECT_KINDS = ("account", "candidate")


class OpsAccount(UUIDPrimaryKey, Timestamped, Base):
    """A BlueLab staff account. Separate surface, separate session (ADR-0010)."""

    __tablename__ = "ops_account"

    email: Mapped[str] = mapped_column(nullable=False)
    display_name: Mapped[str] = mapped_column(nullable=False)
    password_hash: Mapped[str] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(nullable=False, server_default=text("'active'"))

    __table_args__ = (
        enum_check("status", OPS_STATUSES),
        Index("uq_ops_account_email", text("lower(email)"), unique=True),
    )


class OpsAudit(UUIDPrimaryKey, Base):
    """Every ops action, audited: actor, verb, target org, target, reason.

    **Append-only, and the only audit trail in the system** — there are no history
    tables on customer data, because no requirement asks for temporal versioning
    (data/00 §7). The enumerated verb list is the complete set of things ops can
    do; a new verb is a scope change, not a migration detail.
    """

    __tablename__ = "ops_audit"

    ops_account_id: Mapped[UUID] = mapped_column(ForeignKey("ops_account.id"), nullable=False)
    verb: Mapped[str] = mapped_column(nullable=False)
    target_org_id: Mapped[UUID | None] = mapped_column(ForeignKey("org.id"), nullable=True)

    target_ref: Mapped[dict[str, Any]] = mapped_column(
        SNAPSHOT_TYPE, nullable=False, server_default=text("'{}'")
    )
    """**Ids only, never content** (ADR-0010 §3)."""

    reason: Mapped[str] = mapped_column(nullable=False)
    """Mandatory on every mutation — the `reason-required` problem exists to
    enforce it at the API edge (ux/00 §3.3)."""

    occurred_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (
        enum_check("verb", AUDIT_VERBS),
        Index("idx_audit_org_time", "target_org_id", text("occurred_at desc")),
    )


class OpsFault(UUIDPrimaryKey, Base):
    """The fault queue raised by FR-SCR-009 and FR-SCR-013.

    Resolution re-drives the job **by identity** — which is why `detail` carries
    an error class and a retry count and nothing else. Ops must be able to
    diagnose a grading failure without reading the transcript that failed to
    grade (ADR-0010 §4).
    """

    __tablename__ = "ops_fault"

    org_id: Mapped[UUID] = mapped_column(ForeignKey("org.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(nullable=False)
    attempt_id: Mapped[UUID] = mapped_column(ForeignKey("attempt.id"), nullable=False)
    status: Mapped[str] = mapped_column(nullable=False, server_default=text("'open'"))
    detail: Mapped[dict[str, Any]] = mapped_column(
        SNAPSHOT_TYPE, nullable=False, server_default=text("'{}'")
    )
    opened_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    resolved_at: Mapped[datetime | None] = mapped_column(nullable=True)
    resolved_by: Mapped[UUID | None] = mapped_column(ForeignKey("ops_account.id"), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(nullable=True)

    __table_args__ = (
        enum_check("kind", FAULT_KINDS),
        enum_check("status", FAULT_STATUSES),
        Index("idx_fault_queue", "status", "opened_at"),
    )


class ErasureRequest(UUIDPrimaryKey, Base):
    """A CMP-001 erasure execution record (data/03 §3).

    Retained as **evidence** and replayed after a restore (data/05 §4) — a restore
    that resurrected an erased person would be a breach, so the request outlives
    the erasure it performed. Subject references are pseudonymous ids.
    """

    __tablename__ = "erasure_request"

    org_id: Mapped[UUID] = mapped_column(ForeignKey("org.id"), nullable=False, index=True)
    subject_kind: Mapped[str] = mapped_column(nullable=False)
    subject_id: Mapped[UUID] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(nullable=False, server_default=text("'pending'"))
    requested_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    executed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    executed_by: Mapped[UUID | None] = mapped_column(ForeignKey("ops_account.id"), nullable=True)

    evidence: Mapped[dict[str, Any]] = mapped_column(
        SNAPSHOT_TYPE, nullable=False, server_default=text("'{}'")
    )
    """Per-category row and object counts — what was removed, never what it said."""

    __table_args__ = (
        enum_check("subject_kind", SUBJECT_KINDS),
        enum_check("status", ERASURE_STATUSES),
    )


class ExportRequest(UUIDPrimaryKey, Base):
    """A CMP-001 subject-export record (data/03 §4).

    Ops-mediated, never self-service: there are no bulk or export endpoints on the
    customer surface (api/00 §9). The bundle auto-deletes at `expires_at` so a
    subject's whole data set does not sit in the object store indefinitely.
    """

    __tablename__ = "export_request"

    org_id: Mapped[UUID] = mapped_column(ForeignKey("org.id"), nullable=False, index=True)
    subject_kind: Mapped[str] = mapped_column(nullable=False)
    subject_id: Mapped[UUID] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(nullable=False, server_default=text("'pending'"))
    bundle_object_key: Mapped[str | None] = mapped_column(nullable=True)
    requested_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    ready_at: Mapped[datetime | None] = mapped_column(nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        enum_check("subject_kind", SUBJECT_KINDS),
        enum_check("status", EXPORT_STATUSES),
    )
