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

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    LargeBinary,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from bluelab.platform.db.base import Base, Timestamped, UUIDPrimaryKey, enum_check
from bluelab.platform.db.types import SNAPSHOT_TYPE

OPS_STATUSES = ("active", "deactivated")
AUDIT_VERBS = (
    "provision_org",
    "confirm_org_term",
    "renew_org_term",
    "start_org_offboarding",
    "cancel_org_offboarding",
    "extend_org_deadline",
    "create_org_deletion_restriction",
    "release_org_deletion_restriction",
    "record_post_completion_restriction",
    "revise_org_retention_policy",
    "inspect_org_purge_evidence",
    "resolve_org_purge_fault",
    "resolve_subject_request",
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
ERASURE_STATUSES = (
    "pending", "processing", "awaiting_input", "executed", "failed", "rejected", "withdrawn"
)
EXPORT_STATUSES = (
    "pending", "processing", "awaiting_input", "ready", "delivered", "failed", "rejected", "withdrawn"
)
SUBJECT_KINDS = ("account", "candidate")


class OpsAccount(UUIDPrimaryKey, Timestamped, Base):
    """A BlueLab staff account. Separate surface, separate session (ADR-0010)."""

    __tablename__ = "ops_account"

    email: Mapped[str] = mapped_column(nullable=False)
    display_name: Mapped[str] = mapped_column(nullable=False)
    password_hash: Mapped[str] = mapped_column(nullable=False)
    totp_secret_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
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
    target_org_id: Mapped[UUID | None] = mapped_column(nullable=True)

    target_ref: Mapped[dict[str, Any]] = mapped_column(
        SNAPSHOT_TYPE, nullable=False, server_default=text("'{}'")
    )
    """Structural references only: ids and normalized provisioning domains."""

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

    org_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    subject_kind: Mapped[str] = mapped_column(nullable=False)
    subject_id: Mapped[UUID] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(nullable=False, server_default=text("'pending'"))
    request_policy_reference: Mapped[str] = mapped_column(nullable=False)
    response_due_at: Mapped[datetime | None] = mapped_column(nullable=True)
    restriction_id: Mapped[UUID | None] = mapped_column(nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    closed_by: Mapped[UUID | None] = mapped_column(ForeignKey("ops_account.id"), nullable=True)
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

    org_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    subject_kind: Mapped[str] = mapped_column(nullable=False)
    subject_id: Mapped[UUID] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(nullable=False, server_default=text("'pending'"))
    request_policy_reference: Mapped[str] = mapped_column(nullable=False)
    response_due_at: Mapped[datetime | None] = mapped_column(nullable=True)
    restriction_id: Mapped[UUID | None] = mapped_column(nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    closed_by: Mapped[UUID | None] = mapped_column(ForeignKey("ops_account.id"), nullable=True)
    bundle_object_key: Mapped[str | None] = mapped_column(nullable=True)
    requested_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    ready_at: Mapped[datetime | None] = mapped_column(nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        enum_check("subject_kind", SUBJECT_KINDS),
        enum_check("status", EXPORT_STATUSES),
    )


class OrgLifecycleOperation(UUIDPrimaryKey, Timestamped, Base):
    """Pending decision fence and replay identity for lifecycle commands."""

    __tablename__ = "org_lifecycle_operation"

    org_id: Mapped[UUID] = mapped_column(nullable=False)
    service_term_id: Mapped[UUID | None] = mapped_column(nullable=True)
    offboarding_id: Mapped[UUID | None] = mapped_column(nullable=True)
    action: Mapped[str] = mapped_column(nullable=False)
    expected_sequence: Mapped[int] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(nullable=False, server_default=text("'pending'"))
    actor_ops_account_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("ops_account.id"), nullable=True
    )
    reason: Mapped[str] = mapped_column(nullable=False)
    retention_policy_reference: Mapped[str | None] = mapped_column(nullable=True)
    requested_deadline: Mapped[datetime | None] = mapped_column(nullable=True)
    restriction_id: Mapped[UUID | None] = mapped_column(nullable=True)
    command_payload: Mapped[dict[str, Any]] = mapped_column(
        SNAPSHOT_TYPE, nullable=False, server_default=text("'{}'")
    )
    command_digest: Mapped[str | None] = mapped_column(nullable=True)
    resulting_sequence: Mapped[int | None] = mapped_column(nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        enum_check("status", ("pending", "applied", "rejected")),
        enum_check(
            "action",
            (
                "confirm_term", "renew_term", "expire_term", "start", "cancel", "extend",
                "policy_revision", "create_restriction", "release_restriction",
                "set_subject_request_state", "claim",
                "authorize_destructive_step", "complete_destructive_step", "complete",
                "record_post_completion_restriction",
            ),
        ),
        CheckConstraint(
            "(action in ('confirm_term','policy_revision') and offboarding_id is null) "
            "or (action in ('create_restriction','release_restriction',"
            "'set_subject_request_state') "
            "and restriction_id is not null) "
            "or action='renew_term' "
            "or (action='expire_term' and offboarding_id is not null) "
            "or (action not in ('confirm_term','renew_term','expire_term',"
            "'policy_revision','create_restriction','release_restriction',"
            "'set_subject_request_state') "
            "and offboarding_id is not null)",
            name="org_lifecycle_operation_action_scope",
        ),
        Index(
            "uq_org_lifecycle_pending", "org_id", unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )


class OrgDeletionRestriction(UUIDPrimaryKey, Base):
    """Organization-wide purge barrier retained across terms and episodes."""

    __tablename__ = "org_deletion_restriction"

    org_id: Mapped[UUID] = mapped_column(nullable=False)
    offboarding_id: Mapped[UUID | None] = mapped_column(nullable=True)
    scope: Mapped[str] = mapped_column(nullable=False)
    reason: Mapped[str] = mapped_column(nullable=False)
    authority_ref: Mapped[str] = mapped_column(nullable=False)
    release_condition: Mapped[str] = mapped_column(nullable=False)
    related_request_id: Mapped[UUID | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(nullable=False, server_default=text("'active'"))
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    released_at: Mapped[datetime | None] = mapped_column(nullable=True)
    released_by: Mapped[UUID | None] = mapped_column(ForeignKey("ops_account.id"), nullable=True)
    release_reason: Mapped[str | None] = mapped_column(nullable=True)
    release_evidence_reference: Mapped[str | None] = mapped_column(nullable=True)

    __table_args__ = (
        enum_check("status", ("active", "released")),
        CheckConstraint(
            "(status='active' and released_at is null and release_reason is null "
            "and release_evidence_reference is null) or "
            "(status='released' and released_at is not null and release_reason is not null "
            "and release_evidence_reference is not null)",
            name="org_deletion_restriction_resolution",
        ),
        Index(
            "idx_org_deletion_restriction_active", "org_id", "id",
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "uq_org_deletion_restriction_request", "org_id", "related_request_id",
            unique=True, postgresql_where=text("related_request_id is not null"),
        ),
    )


class OrgPurgeRun(UUIDPrimaryKey, Timestamped, Base):
    """Retained organization purge projection keyed by one episode."""

    __tablename__ = "org_purge_run"

    org_id: Mapped[UUID] = mapped_column(nullable=False)
    service_term_id: Mapped[UUID | None] = mapped_column(nullable=True)
    offboarding_id: Mapped[UUID] = mapped_column(nullable=False, unique=True)
    initiating_operator_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("ops_account.id"), nullable=True)
    offboarding_started_at: Mapped[datetime] = mapped_column(nullable=False)
    purge_eligible_at: Mapped[datetime] = mapped_column(nullable=False)
    retention_policy_reference: Mapped[str] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(nullable=False, server_default=text("'pending'"))
    execution_epoch: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"))
    purge_started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    deletion_rule_version: Mapped[str | None] = mapped_column(nullable=True)
    inventory_manifest_key: Mapped[str | None] = mapped_column(nullable=True)
    inventory_manifest_digest: Mapped[str | None] = mapped_column(nullable=True)
    verification_summary: Mapped[dict[str, Any]] = mapped_column(
        SNAPSHOT_TYPE, nullable=False, server_default=text("'{}'"))
    last_failure_class: Mapped[str | None] = mapped_column(nullable=True)
    failure_count: Mapped[int] = mapped_column(nullable=False, server_default=text("0"))
    retry_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_progress_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        enum_check("status", (
            "pending", "running", "paused_restriction", "retry_pending",
            "needs_attention", "completed",
        )),
        CheckConstraint("execution_epoch >= 0", name="org_purge_run_epoch"),
        CheckConstraint(
            "(status='completed' and completed_at is not null) or "
            "(status<>'completed' and completed_at is null)",
            name="org_purge_run_completion",
        ),
    )


class OrgPurgeStep(UUIDPrimaryKey, Base):
    """One bounded deletion batch authorization and its outcome."""

    __tablename__ = "org_purge_step"

    purge_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("org_purge_run.id"), nullable=False)
    step_key: Mapped[str] = mapped_column(nullable=False)
    batch_key: Mapped[str] = mapped_column(nullable=False)
    batch_manifest_key: Mapped[str] = mapped_column(nullable=False)
    batch_manifest_digest: Mapped[str] = mapped_column(nullable=False)
    target_table: Mapped[str | None] = mapped_column(nullable=True)
    batch_keys: Mapped[list[dict[str, Any]]] = mapped_column(
        SNAPSHOT_TYPE, nullable=False, server_default=text("'[]'")
    )
    status: Mapped[str] = mapped_column(nullable=False)
    execution_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    authorized_at: Mapped[datetime] = mapped_column(nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    deleted_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"))
    failure_class: Mapped[str | None] = mapped_column(nullable=True)

    __table_args__ = (
        enum_check("status", ("authorized", "completed", "failed")),
        UniqueConstraint("purge_run_id", "step_key", "batch_key"),
        CheckConstraint("execution_epoch >= 0", name="org_purge_step_epoch"),
        CheckConstraint("deleted_count >= 0", name="org_purge_step_count"),
        CheckConstraint(
            "(status='completed' and completed_at is not null and failure_class is null) "
            "or (status='failed' and failure_class is not null) "
            "or (status='authorized' and completed_at is null and failure_class is null)",
            name="org_purge_step_completion",
        ),
    )
