"""Idempotent reconciliation of provider-authenticated delivery events."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.adapters.email_events import DeliveryState, VerifiedDeliveryEvent
from bluelab.platform.telemetry import metrics

_LOCK_SEND = text(
    "select id, kind, status, last_event_at from email_send"
    " where provider_message_id = :provider_message_id for update"
)
_UPDATE_SEND = text(
    "update email_send set status = :status, last_event_at = :last_event_at"
    " where id = :email_send_id"
)


class ReconciliationOutcome(StrEnum):
    UPDATED = "updated"
    IDEMPOTENT = "idempotent"
    NOT_FOUND = "not_found"
    STALE = "stale"


async def reconcile_delivery_event(
    session: AsyncSession, event: VerifiedDeliveryEvent
) -> ReconciliationOutcome:
    """Apply one verified event without letting delayed events undo a terminal one."""
    if not isinstance(event, VerifiedDeliveryEvent):
        raise TypeError("delivery reconciliation requires an authenticated event")
    send = (
        await session.execute(
            _LOCK_SEND, {"provider_message_id": event.provider_message_id}
        )
    ).mappings().one_or_none()
    if send is None:
        return ReconciliationOutcome.NOT_FOUND
    if send["status"] == event.state.value:
        return ReconciliationOutcome.IDEMPOTENT

    event_at = _as_utc(event.occurred_at)
    current_at = (
        _as_utc(send["last_event_at"])
        if send["last_event_at"] is not None
        else None
    )
    if current_at is not None and event_at < current_at:
        return ReconciliationOutcome.STALE
    if send["status"] in {
        DeliveryState.DELIVERED.value,
        DeliveryState.BOUNCED.value,
    }:
        return ReconciliationOutcome.STALE
    if send["status"] not in {"sent", DeliveryState.DELAYED.value}:
        return ReconciliationOutcome.STALE

    await session.execute(
        _UPDATE_SEND,
        {
            "email_send_id": send["id"],
            "status": event.state.value,
            "last_event_at": event_at,
        },
    )
    metrics.record_email_delivery(kind=str(send["kind"]), state=event.state.value)
    return ReconciliationOutcome.UPDATED


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
