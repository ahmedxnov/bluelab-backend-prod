"""`email_send` — the send ledger with its `(kind, dedupe_key)` uniqueness and its
`sent -> delivered | bounced | delayed` status (data/01 §8).

**The `kind` CHECK is where the closed inventory becomes physical.** Five
templates exist (specs/00 §6); a sixth is a scope change, and the constraint makes
it one — you cannot ship a new email without a migration that visibly widens the
enumeration, which is exactly what architecture/00 §3.3 asks for.

**No recipient addresses and no bodies are stored.** They resolve from the
referenced rows at send time. That is privacy-lean by design: an erased
candidate's address must not survive in a send log, and the ledger's job is to
answer *was this sent* rather than *what did it say*.

`dedupe_key` per kind (api/02 §3):

    E1 → account id, or reset-token id
    E2 → token id — a resend is a new token, hence a new send
    E3 → candidate id
    E4 → shortlist id
    E5 → candidate id
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import ForeignKey, Index, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from bluelab.platform.db.base import Base, Timestamped, UUIDPrimaryKey, enum_check

EMAIL_KINDS = (
    "E1_credentials",
    "E2_invite",
    "E3_candidate_report",
    "E4_shortlist",
    "E5_completion",
)
SEND_STATUSES = ("queued", "sent", "delivered", "bounced", "delayed", "failed")


class EmailSend(UUIDPrimaryKey, Timestamped, Base):
    """One dispatched email."""

    __tablename__ = "email_send"

    org_id: Mapped[UUID] = mapped_column(ForeignKey("org.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(nullable=False)
    dedupe_key: Mapped[str] = mapped_column(nullable=False)

    account_id: Mapped[UUID | None] = mapped_column(ForeignKey("account.id"), nullable=True)
    candidate_id: Mapped[UUID | None] = mapped_column(ForeignKey("candidate.id"), nullable=True)

    token_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("candidate_token.id", ondelete="SET NULL"), nullable=True
    )
    """Nulled rather than cascaded: the send record outlives the token purge and
    erasure, and E-2's `dedupe_key` keeps the id as text so at-most-once holds
    even after the token row is gone."""

    shortlist_id: Mapped[UUID | None] = mapped_column(ForeignKey("shortlist.id"), nullable=True)

    status: Mapped[str] = mapped_column(nullable=False, server_default=text("'queued'"))
    provider_message_id: Mapped[str | None] = mapped_column(nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(nullable=True)

    last_event_at: Mapped[datetime | None] = mapped_column(nullable=True)
    """Ages the sent-without-terminal-event alarm. A silent provider drop is
    caught by the **absence** of a terminal event, not by an error
    (observability/01 §4.3, gate FS-10)."""

    __table_args__ = (
        enum_check("kind", EMAIL_KINDS),
        enum_check("status", SEND_STATUSES),
        # At most one send per (recipient, event) — the dispatcher's idempotency
        # identity (api/02 §2).
        UniqueConstraint("kind", "dedupe_key"),
        Index(
            "idx_email_candidate",
            "candidate_id",
            text("created_at desc"),
            postgresql_where=text("candidate_id is not null"),
        ),
    )
