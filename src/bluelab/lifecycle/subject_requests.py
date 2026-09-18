"""Durable subject-request acceptance and organization-wide purge restrictions."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal
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
from bluelab.modules.operations.schemas import SubjectKind
from bluelab.platform.clock import now
from bluelab.platform.errors import catalog
from bluelab.platform.errors.denial import ProblemError
from bluelab.platform.ids import new_id
from bluelab.platform.queue.catalog import Lane
from bluelab.platform.queue.enqueue import enqueue

RequestKind = Literal["erasure", "export"]
POLICY_REFERENCE = "subject-rights:v1"


def _digest(*, org_id: UUID, kind: RequestKind, subject_kind: SubjectKind,
            subject_id: UUID, reason: str) -> str:
    body = {
        "org_id": str(org_id), "kind": kind, "subject_kind": subject_kind,
        "subject_id": str(subject_id), "reason": reason,
    }
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def prepare(
    session: AsyncSession, *, operation_id: UUID, org_id: UUID,
    kind: RequestKind, subject_kind: SubjectKind, subject_id: UUID,
    actor_id: UUID, reason: str, head: HistoryHead,
) -> OrgLifecycleOperation:
    """Fence claim before any independently accepted restriction can be projected."""
    digest = _digest(
        org_id=org_id, kind=kind, subject_kind=subject_kind,
        subject_id=subject_id, reason=reason,
    )
    existing = (await session.execute(
        select(OrgLifecycleOperation).where(OrgLifecycleOperation.id == operation_id)
    )).scalar_one_or_none()
    if existing is not None:
        if (
            existing.org_id != org_id or existing.action != "create_restriction"
            or existing.actor_ops_account_id != actor_id
            or existing.command_digest != digest
        ):
            raise ProblemError(catalog.OPERATION_ID_REUSE)
        if existing.status == "rejected":
            raise ProblemError(catalog.LIFECYCLE_SEQUENCE_STALE)
        return existing

    projection = await identity_service.lifecycle_projection(session, org_id, lock=True)
    if projection is None:
        raise ProblemError(catalog.SUBJECT_UNKNOWN)
    org = projection[0]
    if org.lifecycle_sequence != head.sequence:
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    pending = (await session.execute(
        select(OrgLifecycleOperation.id).where(
            OrgLifecycleOperation.org_id == org_id,
            OrgLifecycleOperation.status == "pending",
        )
    )).scalar_one_or_none()
    if pending is not None:
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    valid = (await session.execute(
        text("select app_subject_request_valid(:org,:kind,:subject,:erasure)"),
        {"org": org_id, "kind": subject_kind, "subject": subject_id,
         "erasure": kind == "erasure"},
    )).scalar_one()
    if not valid:
        raise ProblemError(catalog.SUBJECT_UNKNOWN)

    request_id, restriction_id, audit_id = new_id(), new_id(), new_id()
    lane = Lane.EXECUTE_ERASURE if kind == "erasure" else Lane.EXECUTE_EXPORT
    data: dict[str, Any] = {
        "request_kind": kind, "request_id": str(request_id),
        "restriction_id": str(restriction_id), "org_id": str(org_id),
        "offboarding_id": str(org.offboarding_id) if org.offboarding_id else None,
        "subject_kind": subject_kind, "subject_id": str(subject_id),
        "request_policy_reference": POLICY_REFERENCE,
        "audit_id": str(audit_id), "initial_status": "pending",
        "restriction_scope": "source_data",
        "authority_ref": POLICY_REFERENCE,
        "release_condition": "request_obligation_resolved",
        "worker_lane": lane.value, "worker_request_id": str(request_id),
    }
    operation = OrgLifecycleOperation(
        id=operation_id, org_id=org_id, offboarding_id=org.offboarding_id,
        action="create_restriction", expected_sequence=org.lifecycle_sequence,
        actor_ops_account_id=actor_id, reason=reason,
        restriction_id=restriction_id, command_payload=data,
        command_digest=digest,
    )
    session.add(operation)
    await session.flush()
    return operation


async def apply(
    session: AsyncSession, *, operation_id: UUID, event: HistoryEvent,
    replay_by_system: bool = False,
) -> UUID:
    """Project restriction, request, audit, and one queued job in one transaction."""
    operation = (await session.execute(
        select(OrgLifecycleOperation).where(OrgLifecycleOperation.id == operation_id)
        .with_for_update()
    )).scalar_one_or_none()
    if operation is None or operation.action != "create_restriction":
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    data = operation.command_payload
    request_id = UUID(data["request_id"])
    if operation.status == "applied":
        return request_id
    if (
        operation.status != "pending" or event.operation_id != operation.id
        or event.org_id != operation.org_id or event.action != operation.action
        or event.data != data or event.reason != operation.reason
        or event.actor_id != operation.actor_ops_account_id
        or event.sequence != operation.expected_sequence + 1
    ):
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    projection = await identity_service.lifecycle_projection(
        session, operation.org_id, lock=True
    )
    if projection is None:
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    org = projection[0]
    if org.lifecycle_sequence != operation.expected_sequence:
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    if data["offboarding_id"] != (
        str(org.offboarding_id) if org.offboarding_id else None
    ):
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    accepted_at = datetime.fromisoformat(event.accepted_at)
    restriction = OrgDeletionRestriction(
        id=UUID(data["restriction_id"]), org_id=operation.org_id,
        offboarding_id=org.offboarding_id, scope=data["restriction_scope"],
        reason=operation.reason, authority_ref=data["authority_ref"],
        release_condition=data["release_condition"],
        related_request_id=request_id, created_at=accepted_at,
    )
    session.add(restriction)
    if data["request_kind"] == "erasure":
        session.add(ErasureRequest(
            id=request_id, org_id=operation.org_id,
            subject_kind=data["subject_kind"], subject_id=UUID(data["subject_id"]),
            status="pending", request_policy_reference=data["request_policy_reference"],
            restriction_id=restriction.id, requested_at=accepted_at,
            executed_by=event.actor_id,
        ))
        audit_verb = "execute_erasure"
    elif data["request_kind"] == "export":
        session.add(ExportRequest(
            id=request_id, org_id=operation.org_id,
            subject_kind=data["subject_kind"], subject_id=UUID(data["subject_id"]),
            status="pending", request_policy_reference=data["request_policy_reference"],
            restriction_id=restriction.id, requested_at=accepted_at,
        ))
        audit_verb = "execute_export"
    else:
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    if event.actor_id is None:
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    audit_ref = {
        "request_id": str(request_id), "restriction_id": str(restriction.id),
        "subject_kind": data["subject_kind"], "subject_id": data["subject_id"],
    }
    if replay_by_system:
        session.add(OpsAudit(
            id=UUID(data["audit_id"]), ops_account_id=event.actor_id,
            verb=audit_verb, target_org_id=operation.org_id,
            target_ref=audit_ref, reason=operation.reason,
            occurred_at=accepted_at,
        ))
    else:
        await session.execute(text(
            "select app_append_ops_audit(:id,:verb,:org,cast(:ref as jsonb),:reason)"
        ), {
            "id": UUID(data["audit_id"]), "verb": audit_verb,
            "org": operation.org_id,
            "ref": json.dumps(audit_ref, sort_keys=True, separators=(",", ":")),
            "reason": operation.reason,
        })
    await session.flush()
    await enqueue(
        session, Lane(data["worker_lane"]), {"request_id": str(request_id)},
        org_id=operation.org_id,
    )
    org.lifecycle_sequence = event.sequence
    operation.status = "applied"
    operation.resulting_sequence = event.sequence
    operation.resolved_at = now()
    return request_id


async def reject(session: AsyncSession, operation_id: UUID) -> None:
    """Release a pending fence after a verified conditional append loss."""
    operation = (await session.execute(
        select(OrgLifecycleOperation).where(OrgLifecycleOperation.id == operation_id)
        .with_for_update()
    )).scalar_one_or_none()
    if operation is not None and operation.status == "pending":
        operation.status = "rejected"
        operation.resolved_at = now()
