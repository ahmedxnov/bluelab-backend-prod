"""Ordered organization deletion restrictions and documented releases."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.adapters.lifecycle_history import HistoryEvent, HistoryHead
from bluelab.modules.identity import service as identity_service
from bluelab.modules.operations.models import (
    ErasureRequest,
    ExportRequest,
    OpsAudit,
    OrgDeletionRestriction,
    OrgLifecycleOperation,
)
from bluelab.modules.operations.schemas import (
    OrgRestrictionCommand,
    OrgRestrictionReleaseCommand,
    OrgRestrictionView,
)
from bluelab.modules.operations.service import require_reason
from bluelab.platform.clock import now
from bluelab.platform.errors import catalog
from bluelab.platform.errors.denial import ProblemError, not_found
from bluelab.platform.ids import new_id


def _digest(org_id: UUID, restriction_id: UUID | None, command: object) -> str:
    payload = command.model_dump(mode="json")  # type: ignore[attr-defined]
    return hashlib.sha256(json.dumps({
        "org_id": str(org_id), "restriction_id": str(restriction_id)
        if restriction_id else None, "command": payload,
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def _pending(session: AsyncSession, org_id: UUID) -> None:
    if await session.scalar(select(OrgLifecycleOperation.id).where(
        OrgLifecycleOperation.org_id == org_id,
        OrgLifecycleOperation.status == "pending",
    )) is not None:
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)


async def prepare_create(
    session: AsyncSession, *, org_id: UUID, offboarding_id: UUID,
    operation_id: UUID, actor_id: UUID, command: OrgRestrictionCommand,
    head: HistoryHead,
) -> OrgLifecycleOperation:
    digest = _digest(org_id, None, command)
    existing = await session.scalar(select(OrgLifecycleOperation).where(
        OrgLifecycleOperation.id == operation_id,
    ))
    if existing is not None:
        if (existing.org_id != org_id or existing.action != "create_restriction"
                or existing.actor_ops_account_id != actor_id
                or existing.command_digest != digest):
            raise ProblemError(catalog.OPERATION_ID_REUSE)
        if existing.status == "rejected":
            raise ProblemError(catalog.LIFECYCLE_SEQUENCE_STALE)
        return existing
    projection = await identity_service.lifecycle_projection(session, org_id, lock=True)
    if projection is None:
        raise not_found()
    org = projection[0]
    if org.lifecycle_sequence != head.sequence:
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    if command.expected_sequence != head.sequence:
        raise ProblemError(catalog.LIFECYCLE_SEQUENCE_STALE)
    if org.offboarding_id != offboarding_id or org.lifecycle_status not in {
        "offboarding", "purging",
    }:
        raise ProblemError(catalog.OFFBOARDING_EPISODE_STALE)
    await _pending(session, org_id)
    if command.related_request_id is not None:
        linked = await session.scalar(select(OrgDeletionRestriction.id).where(
            OrgDeletionRestriction.org_id == org_id,
            OrgDeletionRestriction.related_request_id == command.related_request_id,
        ))
        if linked is not None:
            raise ProblemError(catalog.LIFECYCLE_SEQUENCE_STALE)
        erasure = await session.scalar(select(ErasureRequest.id).where(
            ErasureRequest.id == command.related_request_id,
            ErasureRequest.org_id == org_id,
        ))
        export = await session.scalar(select(ExportRequest.id).where(
            ExportRequest.id == command.related_request_id,
            ExportRequest.org_id == org_id,
        ))
        if erasure is None and export is None:
            raise not_found()
    restriction_id = new_id()
    data: dict[str, Any] = {
        "restriction_id": str(restriction_id), "offboarding_id": str(offboarding_id),
        "scope": command.scope, "authority_ref": command.authority_ref,
        "release_condition": command.release_condition,
        "related_request_id": str(command.related_request_id)
        if command.related_request_id else None,
        "audit_id": str(new_id()),
    }
    operation = OrgLifecycleOperation(
        id=operation_id, org_id=org_id, offboarding_id=offboarding_id,
        action="create_restriction", expected_sequence=head.sequence,
        actor_ops_account_id=actor_id, reason=require_reason(command.reason),
        restriction_id=restriction_id, command_payload=data, command_digest=digest,
    )
    session.add(operation)
    await session.flush()
    return operation


async def prepare_release(
    session: AsyncSession, *, org_id: UUID, restriction_id: UUID,
    operation_id: UUID, actor_id: UUID, command: OrgRestrictionReleaseCommand,
    head: HistoryHead,
) -> OrgLifecycleOperation:
    digest = _digest(org_id, restriction_id, command)
    existing = await session.scalar(select(OrgLifecycleOperation).where(
        OrgLifecycleOperation.id == operation_id,
    ))
    if existing is not None:
        if (existing.org_id != org_id or existing.action != "release_restriction"
                or existing.restriction_id != restriction_id
                or existing.actor_ops_account_id != actor_id
                or existing.command_digest != digest):
            raise ProblemError(catalog.OPERATION_ID_REUSE)
        if existing.status == "rejected":
            raise ProblemError(catalog.LIFECYCLE_SEQUENCE_STALE)
        return existing
    projection = await identity_service.lifecycle_projection(session, org_id, lock=True)
    if projection is None:
        raise not_found()
    org = projection[0]
    if org.lifecycle_sequence != head.sequence:
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    if command.expected_sequence != head.sequence:
        raise ProblemError(catalog.LIFECYCLE_SEQUENCE_STALE)
    await _pending(session, org_id)
    restriction = await session.scalar(select(OrgDeletionRestriction).where(
        OrgDeletionRestriction.id == restriction_id,
        OrgDeletionRestriction.org_id == org_id,
    ).with_for_update())
    if restriction is None:
        raise not_found()
    if restriction.status != "active":
        raise ProblemError(catalog.LIFECYCLE_SEQUENCE_STALE)
    if restriction.related_request_id is not None:
        erasure = await session.scalar(select(ErasureRequest).where(
            ErasureRequest.id == restriction.related_request_id,
            ErasureRequest.org_id == org_id,
        ))
        export = await session.scalar(select(ExportRequest).where(
            ExportRequest.id == restriction.related_request_id,
            ExportRequest.org_id == org_id,
        ))
        if erasure is None and export is None:
            raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
        if (erasure is not None and erasure.status not in {
            "executed", "rejected", "withdrawn",
        }) or (export is not None and export.status not in {
            "delivered", "rejected", "withdrawn",
        }):
            raise ProblemError(catalog.LIFECYCLE_SEQUENCE_STALE)
    data = {
        "restriction_id": str(restriction_id),
        "evidence_reference": command.evidence_reference,
        "audit_id": str(new_id()),
    }
    operation = OrgLifecycleOperation(
        id=operation_id, org_id=org_id, offboarding_id=org.offboarding_id,
        action="release_restriction", expected_sequence=head.sequence,
        actor_ops_account_id=actor_id, reason=require_reason(command.reason),
        restriction_id=restriction_id, command_payload=data, command_digest=digest,
    )
    session.add(operation)
    await session.flush()
    return operation


async def apply(
    session: AsyncSession, *, operation_id: UUID, event: HistoryEvent,
    replay_by_system: bool = False,
) -> UUID:
    operation = await session.scalar(select(OrgLifecycleOperation).where(
        OrgLifecycleOperation.id == operation_id,
    ).with_for_update())
    if operation is None or operation.action not in {
        "create_restriction", "release_restriction",
    }:
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    if operation.status == "applied":
        return UUID(operation.command_payload["restriction_id"])
    if (operation.status != "pending" or event.operation_id != operation_id
            or event.org_id != operation.org_id or event.action != operation.action
            or event.actor_id != operation.actor_ops_account_id
            or event.actor_id is None or event.reason != operation.reason
            or event.sequence != operation.expected_sequence + 1
            or event.data != operation.command_payload):
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    projection = await identity_service.lifecycle_projection(
        session, operation.org_id, lock=True)
    if projection is None or projection[0].lifecycle_sequence != operation.expected_sequence:
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    org = projection[0]
    data = event.data
    restriction_id = UUID(data["restriction_id"])
    accepted_at = datetime.fromisoformat(event.accepted_at)
    if operation.action == "create_restriction":
        accepted_episode = UUID(data["offboarding_id"]) if data["offboarding_id"] else None
        if org.offboarding_id != accepted_episode:
            raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
        session.add(OrgDeletionRestriction(
            id=restriction_id, org_id=org.id, offboarding_id=org.offboarding_id,
            scope=data["scope"], reason=event.reason,
            authority_ref=data["authority_ref"],
            release_condition=data["release_condition"],
            related_request_id=UUID(data["related_request_id"])
            if data["related_request_id"] else None,
            created_at=accepted_at,
        ))
        verb = "create_org_deletion_restriction"
    else:
        restriction = await session.scalar(select(OrgDeletionRestriction).where(
            OrgDeletionRestriction.id == restriction_id,
            OrgDeletionRestriction.org_id == org.id,
        ).with_for_update())
        if restriction is None or restriction.status != "active":
            raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
        restriction.status = "released"
        restriction.released_at = accepted_at
        restriction.released_by = event.actor_id
        restriction.release_reason = event.reason
        restriction.release_evidence_reference = data["evidence_reference"]
        verb = "release_org_deletion_restriction"
    target_ref = {"restriction_id": str(restriction_id)}
    if replay_by_system:
        session.add(OpsAudit(
            id=UUID(data["audit_id"]), ops_account_id=event.actor_id,
            verb=verb, target_org_id=org.id, target_ref=target_ref,
            reason=event.reason, occurred_at=accepted_at,
        ))
    else:
        await session.execute(text(
            "select app_append_ops_audit(:id,:verb,:org,cast(:ref as jsonb),:reason)"
        ), {"id": UUID(data["audit_id"]), "verb": verb, "org": org.id,
            "ref": json.dumps(target_ref), "reason": event.reason})
    org.lifecycle_sequence = event.sequence
    operation.status = "applied"
    operation.resulting_sequence = event.sequence
    operation.resolved_at = now()
    await session.flush()
    return restriction_id


async def view(session: AsyncSession, org_id: UUID, restriction_id: UUID) -> OrgRestrictionView:
    restriction = await session.scalar(select(OrgDeletionRestriction).where(
        OrgDeletionRestriction.org_id == org_id,
        OrgDeletionRestriction.id == restriction_id,
    ))
    if restriction is None:
        raise not_found()
    return OrgRestrictionView.model_validate({
        "id": restriction.id, "org_id": restriction.org_id,
        "offboarding_id": restriction.offboarding_id, "scope": restriction.scope,
        "status": restriction.status, "created_at": restriction.created_at,
        "released_at": restriction.released_at,
        "related_request_id": restriction.related_request_id,
    })
