"""SQLAlchemy models for the tables this module owns (data/01 §2).

Column conventions and the scope-column standard come from
`bluelab.platform.db.base`; nothing here restates them.

Also holds `legal_document_version` (data/01 §1, class P0). The platform-reference
tables have no module of their own, so each lands with the module that reads it —
this one drives the consent and terms gates, so it belongs to identity.

**Team === owning manager.** `account.team_id` is the owning manager's own
`account.id`; a manager's `team_id` equals their `id`. There is no team entity
because the specification defines none (data/00 §3, FR-IDA-009).
"""

from __future__ import annotations

from datetime import datetime
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

LEGAL_KINDS = ("recording_consent_notice", "terms_of_use", "privacy_notice")
ROLES = ("manager", "rep")
CREDENTIAL_INITIAL = "initial"
"""`account.credential_state` while the provisioned password is still in force.

Named here rather than beside either of its readers because both `service.py` (to
decide the gate a new session carries) and `gates.py` (to decide whether an
established one is still behind it) test for it, and a domain fact spelled out
twice is a domain fact that can disagree with itself.
"""

CREDENTIAL_STATES = (CREDENTIAL_INITIAL, "set")
ACCOUNT_STATUSES = ("active", "deactivated")


class LegalDocumentVersion(UUIDPrimaryKey, Timestamped, Base):
    """Notice and terms versions (P0, no org).

    **A new row IS the material change** that re-triggers consent and acceptance
    (CMP-002, CMP-005); a non-material edit does not get a row. The current
    version is `max(effective_at) <= now()`.
    """

    __tablename__ = "legal_document_version"

    kind: Mapped[str] = mapped_column(nullable=False)
    version: Mapped[str] = mapped_column(nullable=False)
    effective_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        enum_check("kind", LEGAL_KINDS),
        UniqueConstraint("kind", "version"),
    )


class Org(UUIDPrimaryKey, Timestamped, Base):
    """The unit of tenancy and isolation (CMP-004). Provisioned by BlueLab ops."""

    __tablename__ = "org"

    name: Mapped[str] = mapped_column(nullable=False)
    timezone: Mapped[str] = mapped_column(nullable=False, server_default=text("'Africa/Cairo'"))
    """The org's civil calendar.

    Month buckets and due-date midnights resolve here, not in UTC — FR-TRM-002's
    "calendar month" means the month the org lived (data/02 §3 V-1). Read by
    `platform.clock.month_window`.
    """


class Account(UUIDPrimaryKey, Mutable, Base):
    """Manager and rep accounts (FR-IDA-001/007).

    Provisioning is operator-mediated; there is no self-signup to model.
    """

    __tablename__ = "account"

    org_id: Mapped[UUID] = mapped_column(ForeignKey("org.id"), nullable=False, index=True)
    team_id: Mapped[UUID] = mapped_column(ForeignKey("account.id"), nullable=False, index=True)

    email: Mapped[str] = mapped_column(nullable=False)
    """The username (FR-IDA-001). Erasure rewrites this to a tombstone rather than
    deleting the row — the statistical residue stands (ADR-0033)."""

    display_name: Mapped[str] = mapped_column(nullable=False)
    role: Mapped[str] = mapped_column(nullable=False)
    password_hash: Mapped[str] = mapped_column(nullable=False)

    credential_state: Mapped[str] = mapped_column(nullable=False, server_default=text("'initial'"))
    """`initial` until the first-sign-in gate completes (FR-IDA-004)."""

    status: Mapped[str] = mapped_column(nullable=False, server_default=text("'active'"))
    deactivated_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        enum_check("role", ROLES),
        enum_check("credential_state", CREDENTIAL_STATES),
        enum_check("status", ACCOUNT_STATUSES),
        # A manager is their own team; a rep never is. This makes "team === owning
        # manager" unrepresentable-if-violated rather than a convention.
        CheckConstraint(
            "(role = 'manager' and team_id = id) or (role = 'rep' and team_id <> id)",
            name="team_is_owning_manager",
        ),
        scope_key("org_id"),
        scope_key("org_id", "team_id"),
        # Platform-wide username, case-insensitive without citext — the extension
        # allowlist is empty (data/00 §2).
        Index("uq_account_email", text("lower(email)"), unique=True),
    )


class ConsentRecord(UUIDPrimaryKey, Base):
    """Recording consent, once per person per notice version (CMP-002).

    Exactly one subject per row — an account **or** a candidate, never both and
    never neither. Insert-only; a replay of the same `(person, version)` is a
    safe no-op via the unique constraints (api/00 §6).
    """

    __tablename__ = "consent_record"

    org_id: Mapped[UUID] = mapped_column(ForeignKey("org.id"), nullable=False, index=True)
    account_id: Mapped[UUID | None] = mapped_column(ForeignKey("account.id"), nullable=True)
    candidate_id: Mapped[UUID | None] = mapped_column(ForeignKey("candidate.id"), nullable=True)
    notice_version: Mapped[str] = mapped_column(nullable=False)
    consented_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("num_nonnulls(account_id, candidate_id) = 1", name="exactly_one_subject"),
        UniqueConstraint("account_id", "notice_version"),
        UniqueConstraint("candidate_id", "notice_version"),
    )


class TermsAcceptance(UUIDPrimaryKey, Base):
    """Terms and privacy acceptance — **a separate instrument from consent**
    (CMP-005).

    Separate table, separate row, separate unique key. The UI must never merge the
    two into one "I agree to everything" control, and modelling them apart makes
    that a schema property rather than a UI convention (ux/01 §5.5).
    """

    __tablename__ = "terms_acceptance"

    org_id: Mapped[UUID] = mapped_column(ForeignKey("org.id"), nullable=False, index=True)
    account_id: Mapped[UUID | None] = mapped_column(ForeignKey("account.id"), nullable=True)
    candidate_id: Mapped[UUID | None] = mapped_column(ForeignKey("candidate.id"), nullable=True)
    terms_version: Mapped[str] = mapped_column(nullable=False)
    privacy_version: Mapped[str] = mapped_column(nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("num_nonnulls(account_id, candidate_id) = 1", name="exactly_one_subject"),
        UniqueConstraint("account_id", "terms_version", "privacy_version"),
        UniqueConstraint("candidate_id", "terms_version", "privacy_version"),
    )


class PasswordResetToken(UUIDPrimaryKey, Timestamped, Base):
    """Password reset (FR-IDA-006): time-limited, single-use, stored hashed.

    Minted by `platform.security.tokens.mint_reset_token`; the plaintext is
    emailed once and never persisted.
    """

    __tablename__ = "password_reset_token"

    account_id: Mapped[UUID] = mapped_column(ForeignKey("account.id"), nullable=False)
    token_hash: Mapped[str] = mapped_column(nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(nullable=True)


class CandidateToken(UUIDPrimaryKey, Base):
    """Candidate access tokens (FR-IDA-011/012/013).

    **Many valid tokens, one candidate.** A resend issues a fresh token and keeps
    the prior one alive (AC-IDA-005) — which is why validation resolves a token
    into its binding rather than asking whether "the" token matches
    (`platform.security.tokens.verify_candidate_token`).

    The single-live-call lease keys on the **participant**, never the token
    (ADR-0011), so one candidate holding three tokens still gets one call.

    Termination on completion (FR-IDA-013) is **derived, not a column**:
    `candidate.completed_at is not null` admits only the completion state, so
    finishing terminates every token at once whatever their count.
    """

    __tablename__ = "candidate_token"

    org_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    team_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    candidate_id: Mapped[UUID] = mapped_column(nullable=False)
    token_hash: Mapped[str] = mapped_column(nullable=False, unique=True)
    issued_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    expires_at: Mapped[datetime] = mapped_column(nullable=False)

    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    """Set in bulk by T-8 on position close (FR-HIR-015)."""

    __table_args__ = (
        scoped_fk(
            columns=("candidate_id", "org_id", "team_id"),
            parent="candidate",
            parent_columns=("id", "org_id", "team_id"),
            on_update="CASCADE",
        ),
        Index("idx_token_candidate", "candidate_id"),
    )
