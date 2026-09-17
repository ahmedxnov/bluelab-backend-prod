"""Procrastinate discovery and durable projection of expired service terms."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, text

from bluelab.adapters.lifecycle_history import (
    HistoryConflict,
    HistoryUnverified,
    LifecycleHistory,
)
from bluelab.lifecycle import service as lifecycle
from bluelab.modules.operations.models import OrgLifecycleOperation
from bluelab.platform.db.privileged import system_scope
from bluelab.platform.db.session import scoped_transaction
from bluelab.platform.telemetry.logging import get_logger

_log = get_logger(__name__)
_DISCOVER = text("select org_id from app_due_org_service_terms(:at, :limit)")
_PENDING = text("select org_id from app_pending_org_term_operations(:limit)")
_DISCOVERY_ORG_ID = UUID(int=0)


async def transition_one(
    org_id: UUID, history: LifecycleHistory, *, at: datetime | None = None
) -> bool:
    """An accepted independent event survives a failed DB projection and replays."""
    head = await history.verified_head(org_id)
    async with scoped_transaction(system_scope(org_id=org_id)) as db:
        operation = await lifecycle.prepare_expiry(
            db, org_id=org_id, head=head, at=at
        )
    if operation is None or operation.status == "applied":
        return False
    if operation.action != "expire_term":
        raise HistoryUnverified("another lifecycle command is pending")
    try:
        event = await history.append(
            org_id=org_id, operation_id=operation.id,
            expected_sequence=operation.expected_sequence,
            action="expire_term", accepted_at=operation.created_at.isoformat(),
            actor_id=None, reason=operation.reason, data=operation.command_payload,
        )
    except HistoryConflict:
        accepted = await history.accepted(org_id, operation.id)
        if accepted is not None:
            event = accepted
        else:
            async with scoped_transaction(system_scope(org_id=org_id)) as db:
                await lifecycle.reject_term(db, operation.id)
            return False
    async with scoped_transaction(system_scope(org_id=org_id)) as db:
        await lifecycle.apply_expiry(db, operation_id=operation.id, event=event)
    return True


async def recover_one(org_id: UUID, history: LifecycleHistory) -> bool:
    """Complete an operator or expiry decision staged before an uncertain write."""
    async with scoped_transaction(system_scope(org_id=org_id)) as db:
        operation = (await db.execute(
            select(OrgLifecycleOperation).where(
                OrgLifecycleOperation.org_id == org_id,
                OrgLifecycleOperation.status == "pending",
            )
        )).scalar_one_or_none()
    if operation is None:
        return False
    if operation.action not in {"confirm_term", "renew_term", "expire_term"}:
        raise HistoryUnverified("unsupported pending lifecycle action")
    accepted = await history.accepted(org_id, operation.id)
    if accepted is None:
        try:
            accepted = await history.append(
                org_id=org_id, operation_id=operation.id,
                expected_sequence=operation.expected_sequence,
                action=operation.action, accepted_at=operation.created_at.isoformat(),
                actor_id=operation.actor_ops_account_id,
                reason=operation.reason, data=operation.command_payload,
            )
        except HistoryConflict:
            accepted = await history.accepted(org_id, operation.id)
            if accepted is None:
                async with scoped_transaction(system_scope(org_id=org_id)) as db:
                    await lifecycle.reject_term(db, operation.id)
                return False
    async with scoped_transaction(system_scope(org_id=org_id)) as db:
        if operation.action == "expire_term":
            await lifecycle.apply_expiry(db, operation_id=operation.id, event=accepted)
        else:
            await lifecycle.apply_term(
                db, operation_id=operation.id, event=accepted,
                replay_by_system=True,
            )
    return True


async def transition_due(
    history: LifecycleHistory, *, at: datetime | None = None
) -> dict[str, int]:
    """Discover due terms hourly; one failure never prevents another transition."""
    cutoff = at or datetime.now(UTC)
    async with scoped_transaction(system_scope(org_id=_DISCOVERY_ORG_ID)) as db:
        pending_ids = [row.org_id for row in (
            await db.execute(_PENDING, {"limit": 1000})
        )]
    transitioned = 0
    failed = 0
    recovered = 0
    for org_id in pending_ids:
        try:
            if await recover_one(org_id, history):
                recovered += 1
        except Exception:  # noqa: BLE001 -- one pending command cannot starve others
            failed += 1
            _log.error("org_term_recovery_failed")
    async with scoped_transaction(system_scope(org_id=_DISCOVERY_ORG_ID)) as db:
        ids = [row.org_id for row in (
            await db.execute(_DISCOVER, {"at": cutoff, "limit": 1000})
        )]
    for org_id in ids:
        try:
            if await transition_one(org_id, history, at=cutoff):
                transitioned += 1
        except Exception:  # noqa: BLE001 -- one tenant cannot starve another
            failed += 1
            _log.error("org_term_transition_failed")
    _log.info(
        "org_term_transition_sweep",
        discovered=len(ids), recovered=recovered,
        transitioned=transitioned, failed=failed,
    )
    if failed:
        raise RuntimeError("one or more organization transitions failed")
    return {
        "discovered": len(ids), "recovered": recovered,
        "transitioned": transitioned, "failed": failed,
    }
