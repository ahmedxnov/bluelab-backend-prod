"""Recover join timeouts, lost terminal callbacks, and stale recordings."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

from sqlalchemy import text
from valkey.asyncio import Valkey

from bluelab.calls.egress import (
    attempt_status,
    delete_orphan_recording,
    reconcile_recording,
)
from bluelab.calls.interruption import Disposition, interrupt
from bluelab.calls.lease import CallLease, CallLeaseStore
from bluelab.calls.registry import CallRegistry, CapacitySlots, SessionState
from bluelab.calls.scope import lifecycle_scope
from bluelab.calls.suspension import CallQuiescenceUnverified, quiesce_org_calls
from bluelab.platform.config import Settings
from bluelab.platform.db.privileged import system_scope
from bluelab.platform.db.session import scoped_transaction
from bluelab.platform.telemetry import metrics
from bluelab.platform.telemetry.logging import get_logger

_log = get_logger(__name__)


async def _service_cutoff(org_id: UUID) -> datetime | None:
    async with scoped_transaction(system_scope(org_id=org_id)) as db:
        row = (await db.execute(text("""
            select o.service_ends_at, o.offboarding_started_at,
                   o.lifecycle_status,
                   (select min(p.created_at) from org_lifecycle_operation p
                     where p.org_id=o.id and p.status='pending'
                       and p.action='start') as pending_start
              from org o where o.id=:org
        """), {"org": org_id})).mappings().one_or_none()
    if row is None:
        return datetime.now(UTC) - timedelta(seconds=60)
    if row["pending_start"] is not None:
        return cast(datetime, row["pending_start"])
    if row["lifecycle_status"] != "active":
        return cast(datetime | None, row["offboarding_started_at"]) or (
            datetime.now(UTC) - timedelta(seconds=60))
    if row["service_ends_at"] is not None and row["service_ends_at"] <= datetime.now(UTC):
        return cast(datetime, row["service_ends_at"])
    return None


async def run_once(client: Valkey, settings: Settings) -> int:
    """Apply every due recovery idempotently and return the processed count."""
    registry = CallRegistry(client)
    leases = CallLeaseStore(client, ttl_seconds=settings.call_lease_seconds)
    slots = CapacitySlots(client, capacity=settings.call_capacity)
    processed = 0
    for call in await registry.due():
        if call.state is SessionState.TERMINAL:
            continue
        disposition = (
            Disposition.NEVER_ESTABLISHED
            if call.state is SessionState.PENDING
            else Disposition.INSTANCE_LOST
        )
        if call.attempt_id is not None:
            async with scoped_transaction(lifecycle_scope(call)) as db:
                await interrupt(
                    db,
                    attempt_id=call.attempt_id,
                    drill_id=call.drill_id,
                    rep_account_id=None
                    if call.participant_kind == "candidate"
                    else call.participant_id,
                    disposition=disposition,
                    candidate_id=call.participant_id
                    if call.participant_kind == "candidate"
                    else None,
                    org_id=call.org_id,
                    team_id=call.team_id,
                )
        lease = CallLease.pending(
            participant_kind=call.participant_kind,
            participant_id=call.participant_id,
            call_id=call.call_id,
            attempt_id=call.attempt_id,
        )
        await leases.release(lease)
        await slots.release(call.call_id, call.capacity_slot)
        await registry.mark_terminal(call)
        metrics.record_call_recovery(outcome="lease_expired")
        metrics.record_call_disposition(
            outcome="never_established"
            if disposition is Disposition.NEVER_ESTABLISHED
            else "interrupted"
        )
        processed += 1
    for call in await registry.due_recordings():
        async with scoped_transaction(lifecycle_scope(call)) as db:
            status = await attempt_status(db, call)
            if status != "interrupted":
                await reconcile_recording(db, call, available=False)
        if status == "interrupted":
            await delete_orphan_recording(settings, call)
        processed += 1
    for org_id in await registry.active_org_ids():
        cutoff = await _service_cutoff(org_id)
        if cutoff is None:
            continue
        try:
            await quiesce_org_calls(org_id, since=cutoff, valkey=client, settings=settings)
            metrics.record_call_recovery(outcome="service_suspended")
        except CallQuiescenceUnverified:
            _log.error("service_call_quiescence_failed")
            metrics.record_call_recovery(outcome="service_quiescence_failed")
    return processed
