"""Ordered operator decisions on subject-rights requests."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.adapters.erasure_ledger import ErasureLedger
from bluelab.adapters.lifecycle_history import HistoryEvent, HistoryHead
from bluelab.modules.identity import service as identity_service
from bluelab.modules.operations.models import (
    ErasureRequest,
    ExportRequest,
    OpsAudit,
    OrgDeletionRestriction,
    OrgLifecycleOperation,
)
from bluelab.modules.operations.schemas import SubjectRequestStateCommand
from bluelab.platform.clock import now
from bluelab.platform.errors import catalog
from bluelab.platform.errors.denial import ProblemError, not_found
from bluelab.platform.ids import new_id
from bluelab.platform.queue.catalog import Lane
from bluelab.platform.queue.enqueue import enqueue

RequestKind = Literal["erasure", "export"]
_TERMINAL = frozenset({"rejected", "withdrawn", "delivered"})


def _allowed(kind: RequestKind, old: str, new: str) -> bool:
    if new == "awaiting_input":
        return old in {"pending", "failed"}
    if new == "pending":
        return old in {"awaiting_input", "failed"}
    if new == "delivered":
        return kind == "export" and old == "ready"
    if new in {"rejected", "withdrawn"}:
        return old in ({"pending", "awaiting_input", "failed", "ready"}
                       if kind == "export" else {"pending", "awaiting_input", "failed"})
    return False


async def prepare(
    session: AsyncSession, *, kind: RequestKind, request_id: UUID,
    operation_id: UUID, actor_id: UUID, command: SubjectRequestStateCommand,
    head: HistoryHead, ledger: ErasureLedger,
) -> OrgLifecycleOperation:
    """Stage a decision while excluding the request worker and purge claim."""
    model = ErasureRequest if kind == "erasure" else ExportRequest
    request = cast(ErasureRequest | ExportRequest | None, (await session.execute(
        select(model).where(model.id == request_id)
    )).scalar_one_or_none())
    if request is None:
        raise not_found()
    org_id = request.org_id
    digest = hashlib.sha256(json.dumps({
        "kind": kind, "request_id": str(request_id),
        "command": command.model_dump(mode="json"),
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    existing = (await session.execute(select(OrgLifecycleOperation).where(
        OrgLifecycleOperation.id == operation_id,
    ))).scalar_one_or_none()
    if existing is not None:
        if (
            existing.org_id != org_id or existing.actor_ops_account_id != actor_id
            or existing.command_digest != digest
        ):
            raise ProblemError(catalog.OPERATION_ID_REUSE)
        if existing.status == "rejected":
            raise ProblemError(catalog.LIFECYCLE_SEQUENCE_STALE)
        return existing
    projection = await identity_service.lifecycle_projection(session, org_id, lock=True)
    if projection is None:
        raise not_found()
    org = projection[0]
    request = cast(ErasureRequest | ExportRequest | None, (await session.execute(
        select(model).where(model.id == request_id, model.org_id == org_id)
        .with_for_update()
    )).scalar_one_or_none())
    if request is None:
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    if org.lifecycle_sequence != head.sequence:
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    if (await session.scalar(select(OrgLifecycleOperation.id).where(
        OrgLifecycleOperation.org_id == org_id,
        OrgLifecycleOperation.status == "pending",
    ))) is not None:
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    if request.status != command.expected_status or not _allowed(
        kind, request.status, command.status
    ):
        raise ProblemError(catalog.LIFECYCLE_SEQUENCE_STALE)
    restriction = (await session.execute(select(OrgDeletionRestriction).where(
        OrgDeletionRestriction.id == request.restriction_id,
        OrgDeletionRestriction.org_id == org_id,
    ).with_for_update())).scalar_one_or_none()
    if restriction is None or restriction.status != "active" or (
        restriction.related_request_id != request_id
    ):
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    terminal = command.status in _TERMINAL
    if kind == "erasure" and terminal and await ledger.is_armed(request_id):
        raise ProblemError(catalog.LIFECYCLE_SEQUENCE_STALE)
    # A bundle or outstanding capability cannot be silently abandoned.
    if (
        kind == "export" and request.status == "ready"
        and command.status in {"rejected", "withdrawn"}
        and cast(ExportRequest, request).bundle_object_key is not None
    ):
        raise ProblemError(catalog.LIFECYCLE_SEQUENCE_STALE)
    data = {
        "request_kind": kind, "request_id": str(request_id),
        "restriction_id": str(restriction.id),
        "expected_status": command.expected_status, "status": command.status,
        "response_due_at": None,
        "evidence_reference": command.evidence_reference,
        "audit_id": str(new_id()),
        "worker_lane": (Lane.EXECUTE_ERASURE if kind == "erasure" else Lane.EXECUTE_EXPORT).value
        if command.status == "pending" else None,
    }
    operation = OrgLifecycleOperation(
        id=operation_id, org_id=org_id, offboarding_id=org.offboarding_id,
        action="release_restriction" if terminal else "set_subject_request_state",
        expected_sequence=head.sequence, actor_ops_account_id=actor_id,
        reason=command.reason, restriction_id=restriction.id,
        command_payload=data, command_digest=digest,
    )
    session.add(operation)
    await session.flush()
    if command.status == "awaiting_input":
        operation.command_payload = {
            **data,
            "response_due_at": (operation.created_at + timedelta(days=30)).isoformat(),
        }
    return operation


async def apply(
    session: AsyncSession, *, operation_id: UUID, event: HistoryEvent,
    replay_by_system: bool = False,
) -> tuple[RequestKind, UUID]:
    """Project the state, restriction outcome, audit, and enqueue together."""
    operation = (await session.execute(select(OrgLifecycleOperation).where(
        OrgLifecycleOperation.id == operation_id,
    ).with_for_update())).scalar_one_or_none()
    if operation is None or operation.action not in {
        "set_subject_request_state", "release_restriction",
    }:
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    data = operation.command_payload
    kind: RequestKind = data["request_kind"]
    request_id = UUID(data["request_id"])
    if operation.status == "applied":
        return kind, request_id
    if (
        operation.status != "pending" or event.operation_id != operation_id
        or event.org_id != operation.org_id or event.action != operation.action
        or event.actor_id != operation.actor_ops_account_id or event.actor_id is None
        or event.reason != operation.reason or event.data != data
        or event.sequence != operation.expected_sequence + 1
    ):
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    projection = await identity_service.lifecycle_projection(
        session, operation.org_id, lock=True
    )
    if projection is None or projection[0].lifecycle_sequence != operation.expected_sequence:
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    org = projection[0]
    model = ErasureRequest if kind == "erasure" else ExportRequest
    request = cast(ErasureRequest | ExportRequest | None, (await session.execute(select(model).where(
        model.id == request_id, model.org_id == operation.org_id,
    ).with_for_update())).scalar_one_or_none())
    if request is None or request.status != data["expected_status"]:
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    restriction = (await session.execute(select(OrgDeletionRestriction).where(
        OrgDeletionRestriction.id == operation.restriction_id,
        OrgDeletionRestriction.org_id == operation.org_id,
    ).with_for_update())).scalar_one_or_none()
    if restriction is None or restriction.status != "active":
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    accepted_at = datetime.fromisoformat(event.accepted_at)
    request.status = data["status"]
    request.response_due_at = (
        datetime.fromisoformat(data["response_due_at"])
        if data["response_due_at"] else None
    )
    if data["status"] in _TERMINAL:
        request.closed_at = accepted_at
        request.closed_by = event.actor_id
        restriction.status = "released"
        restriction.released_at = accepted_at
        restriction.released_by = event.actor_id
        restriction.release_reason = event.reason
        restriction.release_evidence_reference = data["evidence_reference"]
        if kind == "export" and data["status"] == "delivered":
            cast(ExportRequest, request).delivered_at = accepted_at
    if data["worker_lane"] is not None:
        await enqueue(session, Lane(data["worker_lane"]),
                      {"request_id": str(request_id)}, org_id=org.id)
    audit_ref = {
        "request_id": str(request_id), "restriction_id": str(restriction.id),
        "request_kind": kind, "status": data["status"],
        "evidence_reference": data["evidence_reference"],
    }
    if replay_by_system:
        session.add(OpsAudit(
            id=UUID(data["audit_id"]), ops_account_id=event.actor_id,
            verb="resolve_subject_request", target_org_id=org.id,
            target_ref=audit_ref, reason=event.reason,
            occurred_at=accepted_at,
        ))
    else:
        await session.execute(text(
            "select app_append_ops_audit(:id,'resolve_subject_request',"
            ":org,cast(:ref as jsonb),:reason)"
        ), {
            "id": UUID(data["audit_id"]), "org": org.id,
            "ref": json.dumps(audit_ref, sort_keys=True, separators=(",", ":")),
            "reason": event.reason,
        })
    org.lifecycle_sequence = event.sequence
    operation.status = "applied"
    operation.resulting_sequence = event.sequence
    operation.resolved_at = now()
    await session.flush()
    return kind, request_id


async def reject(session: AsyncSession, operation_id: UUID) -> None:
    operation = (await session.execute(select(OrgLifecycleOperation).where(
        OrgLifecycleOperation.id == operation_id,
    ).with_for_update())).scalar_one_or_none()
    if operation is not None and operation.status == "pending":
        operation.status = "rejected"
        operation.resolved_at = now()
