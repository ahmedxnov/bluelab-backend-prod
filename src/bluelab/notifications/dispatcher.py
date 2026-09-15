"""Closed notification-ledger dispatch and E-1 delivery.

The send row is locked while current recipient state is resolved and transport
acceptance is recorded. A duplicate queue job therefore observes ``sent`` and
does no transport work. The short-lived secret is deleted in the same commit as
acceptance, or by the terminal-failure action.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.adapters.email import EmailTransport
from bluelab.adapters.object_store import ObjectRef, ObjectStore
from bluelab.adapters.secrets import (
    DeliverySecretContext,
    SecretEnvelopeError,
    SecretUnsealer,
)
from bluelab.notifications.templates import (
    E1Purpose,
    EmailKind,
    render_e1,
    render_e2,
    render_report_email,
)
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
    "select id, org_id, kind, status, account_id, candidate_id, token_id"
    " from email_send where id = :email_send_id for update"
)
_E2_RECIPIENT = text("""select c.email,c.name,p.title,o.name org_name,t.expires_at
 from candidate c join position p on p.id=c.position_id join org o on o.id=c.org_id
 join candidate_token t on t.id=:token_id
 where c.id=:candidate_id and c.org_id=:org_id""")
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


async def dispatch_e2(session: AsyncSession, *, email_send_id: UUID, unsealer: SecretUnsealer,
                      transport: EmailTransport, sender: str, public_app_url: str) -> DispatchOutcome:
    """Dispatch one sealed E2 invitation without recovering a token from its hash."""
    send = (await session.execute(_LOCK_SEND, {"email_send_id": email_send_id})).mappings().one_or_none()
    if send is None:
        return DispatchOutcome.SUBJECT_ABSENT
    if send["status"] != "queued":
        return DispatchOutcome.ALREADY_FINAL
    if str(send["kind"]) != EmailKind.E2_INVITE or send["candidate_id"] is None or send["token_id"] is None:
        raise TerminalDispatchError("email kind has no E-2 recipient")
    recipient = (await session.execute(_E2_RECIPIENT, {"candidate_id": send["candidate_id"], "token_id": send["token_id"], "org_id": send["org_id"]})).mappings().one_or_none()
    if recipient is None:
        return DispatchOutcome.SUBJECT_ABSENT
    secret = (await session.execute(_LOAD_SECRET, {"email_send_id": email_send_id})).mappings().one_or_none()
    if secret is None or _as_utc(secret["expires_at"]) <= now() or str(secret["purpose"]) != "candidate_invite_token":
        raise TerminalDispatchError("E-2 delivery secret is absent or expired")
    try:
        envelope = json.loads(await unsealer.unseal(bytes(secret["ciphertext"]), context=DeliverySecretContext(email_send_id=email_send_id, org_id=UUID(str(send["org_id"])), purpose="candidate_invite_token")))
        token = envelope["token"]
        template = envelope.get("template")
        if not isinstance(token, str) or (template is not None and not isinstance(template, str)):
            raise ValueError("invalid envelope")
    except (SecretEnvelopeError, ValueError, json.JSONDecodeError, KeyError) as exc:
        raise TerminalDispatchError("E-2 delivery secret is unauthentic") from exc
    message = render_e2(email_send_id=email_send_id, recipient=str(recipient["email"]), recipient_name=str(recipient["name"]),
                        org_name=str(recipient["org_name"]), position_title=str(recipient["title"]),
                        expires_at=_as_utc(recipient["expires_at"]).isoformat(), token=token, template=template,
                        sender=sender, public_app_url=public_app_url)
    provider_message_id = await transport.send(message)
    if not provider_message_id or len(provider_message_id) > 512:
        raise TerminalDispatchError("email provider message identity is invalid")
    accepted_at = now()
    await session.execute(_MARK_SENT, {"email_send_id":email_send_id,"provider_message_id":provider_message_id,"accepted_at":accepted_at})
    await session.execute(_DELETE_SECRET, {"email_send_id":email_send_id})
    metrics.record_email_delivery(kind=EmailKind.E2_INVITE.value, state="sent")
    return DispatchOutcome.SENT


async def dispatch_e3_or_e4(session: AsyncSession, *, email_send_id: UUID, transport: EmailTransport, sender: str, object_store: ObjectStore) -> DispatchOutcome:
    """Send only ready, single-format report PDFs; a queued duplicate locks out."""
    send = (await session.execute(_LOCK_SEND, {"email_send_id": email_send_id})).mappings().one_or_none()
    if send is None: return DispatchOutcome.SUBJECT_ABSENT
    if send["status"] != "queued": return DispatchOutcome.ALREADY_FINAL
    kind = str(send["kind"])
    if kind == EmailKind.E3_CANDIDATE_REPORT:
        row = (await session.execute(text("""select c.email,c.name,p.title,cr.pdf_object_key,cr.pdf_status,c.id,c.org_id from candidate c join position p on p.id=c.position_id join candidate_report cr on cr.candidate_id=c.id where c.id=:id"""), {"id":send["candidate_id"]})).mappings().one_or_none()
        if row is None: return DispatchOutcome.SUBJECT_ABSENT
        if row["pdf_status"] != "available": raise TerminalDispatchError("report PDF is not ready")
        ref = ObjectRef.report(org_id=UUID(str(row["org_id"])), candidate_id=UUID(str(row["id"])))
        if ref.key != row["pdf_object_key"]: raise TerminalDispatchError("report object key is invalid")
        attachment = (f"candidate-report-{row['id']}.pdf", await object_store.get(ref), "application/pdf")
        message = render_report_email(email_send_id=email_send_id, recipient=str(row["email"]), recipient_name=str(row["name"]), position_title=str(row["title"]), sender=sender, attachments=(attachment,))
    elif kind == EmailKind.E4_SHORTLIST:
        row = (await session.execute(text("select recipients,email_body,candidate_snapshot from shortlist where id=:id"), {"id":send["shortlist_id"]})).mappings().one_or_none()
        if row is None: return DispatchOutcome.SUBJECT_ABSENT
        recipients = row["recipients"]
        snapshot = row["candidate_snapshot"]
        if not isinstance(recipients, list) or not recipients or not isinstance(snapshot, list) or not snapshot: raise TerminalDispatchError("shortlist snapshot is incomplete")
        attachments = []
        for item in snapshot:
            candidate_id = UUID(str(item["candidate_id"])); ref = ObjectRef(str(item["pdf_object_key"]))
            if ref != ObjectRef.report(org_id=UUID(str(send["org_id"])), candidate_id=candidate_id): raise TerminalDispatchError("shortlist report key is invalid")
            attachments.append((f"candidate-report-{candidate_id}.pdf", await object_store.get(ref), "application/pdf"))
        message = render_report_email(email_send_id=email_send_id, recipient=str(recipients[0]), recipient_name="HR", position_title="Shortlist", sender=sender, body=str(row["email_body"]), attachments=tuple(attachments), bcc_recipients=tuple(str(value) for value in recipients[1:]))
    else:
        raise TerminalDispatchError("email kind has no report dispatcher")
    provider_message_id = await transport.send(message)
    if not provider_message_id or len(provider_message_id) > 512: raise TerminalDispatchError("email provider message identity is invalid")
    accepted_at = now(); await session.execute(_MARK_SENT, {"email_send_id":email_send_id,"provider_message_id":provider_message_id,"accepted_at":accepted_at})
    metrics.record_email_delivery(kind=kind, state="sent")
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
