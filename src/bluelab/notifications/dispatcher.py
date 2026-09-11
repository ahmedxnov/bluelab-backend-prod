"""Closed notification-ledger dispatch and E-1 delivery.

The send row is locked while current recipient state is resolved and transport
acceptance is recorded. A duplicate queue job therefore observes ``sent`` and
does no transport work. The short-lived secret is deleted in the same commit as
acceptance, or by the terminal-failure action.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.adapters.email import EmailTransport
from bluelab.adapters.secrets import (
    DeliverySecretContext,
    SecretEnvelopeError,
    SecretUnsealer,
)
from bluelab.notifications.templates import E1Purpose, EmailKind, render_e1
from bluelab.platform.clock import now
from bluelab.platform.telemetry import metrics


class E1Recipient(Protocol):
    """Structural identity projection supplied through its public service."""

    @property
    def email(self) -> str: ...

    @property
    def display_name(self) -> str: ...


RecipientResolver = Callable[..., Awaitable[E1Recipient | None]]

_LOCK_SEND = text(
    "select id, org_id, kind, status, account_id"
    " from email_send where id = :email_send_id for update"
)
_LOAD_SECRET = text(
    "select purpose, ciphertext, expires_at"
    " from email_delivery_secret where email_send_id = :email_send_id"
)
_MARK_SENT = text(
    "update email_send set status = 'sent', provider_message_id = :provider_message_id,"
    " sent_at = :accepted_at, last_event_at = :accepted_at"
    " where id = :email_send_id and status = 'queued'"
)
_MARK_FAILED = text(
    "update email_send set status = 'failed', last_event_at = :failed_at"
    " where id = :email_send_id and status = 'queued' returning kind"
)
_DELETE_SECRET = text(
    "delete from email_delivery_secret where email_send_id = :email_send_id"
)


class DispatchOutcome(StrEnum):
    SENT = "sent"
    ALREADY_FINAL = "already_final"
    SUBJECT_ABSENT = "subject_absent"


class TerminalDispatchError(RuntimeError):
    """Current state makes this send permanently impossible."""


async def dispatch_e1(
    session: AsyncSession,
    *,
    email_send_id: UUID,
    unsealer: SecretUnsealer,
    transport: EmailTransport,
    recipient_resolver: RecipientResolver,
    sender: str,
    public_app_url: str,
) -> DispatchOutcome:
    """Resolve, render, and accept one E-1 send with duplicate-job exclusion."""
    send = (
        await session.execute(_LOCK_SEND, {"email_send_id": email_send_id})
    ).mappings().one_or_none()
    if send is None:
        # Erasure/offboarding may legitimately win the race with a queued job.
        return DispatchOutcome.SUBJECT_ABSENT
    if send["status"] != "queued":
        return DispatchOutcome.ALREADY_FINAL

    try:
        kind = EmailKind(str(send["kind"]))
    except ValueError as exc:
        raise TerminalDispatchError("email kind is outside the closed inventory") from exc
    if kind is not EmailKind.E1_CREDENTIALS or send["account_id"] is None:
        raise TerminalDispatchError("email kind has no active dispatcher")

    recipient = await recipient_resolver(
        session,
        account_id=UUID(str(send["account_id"])),
        org_id=UUID(str(send["org_id"])),
    )
    if recipient is None:
        raise TerminalDispatchError("E-1 recipient is not active")

    delivery_secret = (
        await session.execute(_LOAD_SECRET, {"email_send_id": email_send_id})
    ).mappings().one_or_none()
    if delivery_secret is None:
        raise TerminalDispatchError("E-1 delivery secret is absent")
    if _as_utc(delivery_secret["expires_at"]) <= now():
        raise TerminalDispatchError("E-1 delivery secret has expired")
    try:
        purpose = E1Purpose(str(delivery_secret["purpose"]))
    except ValueError as exc:
        raise TerminalDispatchError("E-1 purpose is invalid") from exc

    try:
        plaintext = await unsealer.unseal(
            bytes(delivery_secret["ciphertext"]),
            context=DeliverySecretContext(
                email_send_id=email_send_id,
                org_id=UUID(str(send["org_id"])),
                purpose=purpose.value,
            ),
        )
    except SecretEnvelopeError as exc:
        raise TerminalDispatchError("E-1 delivery secret is unauthentic") from exc

    message = render_e1(
        email_send_id=email_send_id,
        recipient=recipient.email,
        recipient_name=recipient.display_name,
        sender=sender,
        purpose=purpose,
        secret=plaintext,
        public_app_url=public_app_url,
    )
    provider_message_id = await transport.send(message)
    if not provider_message_id or len(provider_message_id) > 512:
        raise TerminalDispatchError("email provider message identity is invalid")

    accepted_at = now()
    await session.execute(
        _MARK_SENT,
        {
            "email_send_id": email_send_id,
            "provider_message_id": provider_message_id,
            "accepted_at": accepted_at,
        },
    )
    await session.execute(_DELETE_SECRET, {"email_send_id": email_send_id})
    metrics.record_email_delivery(kind=kind.value, state="sent")
    return DispatchOutcome.SENT


async def mark_dispatch_failed(session: AsyncSession, *, email_send_id: UUID) -> None:
    """Commit the lane's terminal state and destroy any remaining plaintext envelope."""
    await session.execute(_LOCK_SEND, {"email_send_id": email_send_id})
    kind = (
        await session.execute(
            _MARK_FAILED,
            {"email_send_id": email_send_id, "failed_at": now()},
        )
    ).scalar_one_or_none()
    if kind is not None:
        metrics.record_email_delivery(kind=str(kind), state="failed")
    await session.execute(_DELETE_SECRET, {"email_send_id": email_send_id})


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
