"""Pending, independently sequenced organization service-term decisions."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any, Literal, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.adapters.lifecycle_history import HistoryEvent, HistoryHead
from bluelab.modules.identity import service as identity_service
from bluelab.modules.identity.models import OrgRetentionPolicy, OrgServiceTerm
from bluelab.modules.operations.models import (
    OpsAudit,
    OrgDeletionRestriction,
    OrgLifecycleOperation,
)
from bluelab.modules.operations.schemas import (
    OrgDeadlineCommand,
    OrgLifecycleCommand,
    OrgLifecycleView,
    OrgRestrictionView,
    OrgRetentionPolicyCommand,
    OrgRetentionPolicyView,
    OrgServiceTermCommand,
    RetentionUnit,
)
from bluelab.modules.operations.service import append_audit, require_reason
from bluelab.platform.clock import now, retention_deadline, service_term_bounds
from bluelab.platform.errors import catalog
from bluelab.platform.errors.denial import ProblemError, not_found
from bluelab.platform.ids import new_id

DEFAULT_POLICY_REFERENCE = "org-default-90d:v1"
EXPIRE_REASON = "Confirmed service term expired"


async def _term_policy(
    session: AsyncSession, term: OrgServiceTerm,
) -> tuple[int, RetentionUnit, str | None]:
    """Read the immutable policy applied to this term, not today's revision."""
    operation = (await session.execute(
        select(OrgLifecycleOperation).where(
            OrgLifecycleOperation.org_id == term.org_id,
            OrgLifecycleOperation.service_term_id == term.id,
            OrgLifecycleOperation.status == "applied",
            OrgLifecycleOperation.action.in_(("confirm_term", "renew_term")),
        )
    )).scalar_one_or_none()
    if operation is None:
        raise ProblemError(catalog.RETENTION_POLICY_UNVERIFIED)
    policy = operation.command_payload.get("retention_policy")
    if not isinstance(policy, dict) or policy.get("policy_reference") != term.retention_policy_reference:
        raise ProblemError(catalog.RETENTION_POLICY_UNVERIFIED)
    return (
        int(policy["period_value"]),
        cast(RetentionUnit, policy["period_unit"]),
        cast(str | None, policy.get("calendar_timezone")),
    )


def expiry_operation_id(org_id: UUID, service_term_id: UUID) -> UUID:
    """One stable transition identity per confirmed term across scheduler retries."""
    return uuid5(NAMESPACE_URL, f"bluelab:expire-term:{org_id}:{service_term_id}")


async def prepare_expiry(
    session: AsyncSession, *, org_id: UUID, head: HistoryHead,
    at: datetime | None = None,
) -> OrgLifecycleOperation | None:
    """Stage one due transition; its deadline comes from the term, not job time."""
    projection = await identity_service.lifecycle_projection(session, org_id, lock=True)
    if projection is None:
        return None
    org, term, _ = projection
    if org.lifecycle_status != "active" or term is None or term.ends_at > (at or now()):
        return None
    operation_id = expiry_operation_id(org_id, term.id)
    existing = (await session.execute(
        select(OrgLifecycleOperation).where(OrgLifecycleOperation.id == operation_id)
    )).scalar_one_or_none()
    if existing is not None:
        return existing
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
    period_value, period_unit, calendar_timezone = await _term_policy(session, term)
    deadline = retention_deadline(
        term.ends_at, period_value, period_unit, calendar_timezone
    )
    episode_id = new_id()
    data: dict[str, Any] = {
        "service_term_id": str(term.id), "offboarding_id": str(episode_id),
        "hold_started_at": term.ends_at.isoformat(),
        "purge_eligible_at": deadline.isoformat(),
        "retention_policy_reference": term.retention_policy_reference,
    }
    operation = OrgLifecycleOperation(
        id=operation_id, org_id=org_id, service_term_id=term.id,
        offboarding_id=episode_id, action="expire_term",
        expected_sequence=org.lifecycle_sequence, actor_ops_account_id=None,
        reason=EXPIRE_REASON, retention_policy_reference=term.retention_policy_reference,
        requested_deadline=deadline, command_payload=data,
        command_digest=hashlib.sha256(
            json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    )
    session.add(operation)
    await session.flush()
    return operation


async def apply_expiry(
    session: AsyncSession, *, operation_id: UUID, event: HistoryEvent
) -> None:
    """Project a durably accepted expiry, preserving the original cutoff."""
    operation = (await session.execute(
        select(OrgLifecycleOperation).where(OrgLifecycleOperation.id == operation_id)
        .with_for_update()
    )).scalar_one_or_none()
    if operation is None or operation.action != "expire_term":
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    if operation.status == "applied":
        return
    if (
        operation.status != "pending" or event.operation_id != operation.id
        or event.org_id != operation.org_id or event.actor_id is not None
        or event.action != "expire_term" or event.data != operation.command_payload
        or event.reason != operation.reason
        or event.sequence != operation.expected_sequence + 1
    ):
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    projection = await identity_service.lifecycle_projection(
        session, operation.org_id, lock=True
    )
    if projection is None:
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    org, term, _ = projection
    if (
        org.lifecycle_sequence != operation.expected_sequence
        or org.lifecycle_status != "active" or term is None
        or term.id != operation.service_term_id
        or term.ends_at.isoformat() != event.data["hold_started_at"]
    ):
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    org.lifecycle_status = "offboarding"
    org.lifecycle_sequence = event.sequence
    org.offboarding_id = UUID(event.data["offboarding_id"])
    org.offboarding_started_at = datetime.fromisoformat(event.data["hold_started_at"])
    org.offboarding_started_by = None
    org.purge_eligible_at = datetime.fromisoformat(event.data["purge_eligible_at"])
    org.retention_policy_reference = event.data["retention_policy_reference"]
    operation.status = "applied"
    operation.resulting_sequence = event.sequence
    operation.resolved_at = now()


def _command_digest(
    org_id: UUID, command: OrgServiceTermCommand | OrgLifecycleCommand,
) -> str:
    serialized = json.dumps(
        {"org_id": str(org_id), "command": command.model_dump(mode="json")},
        sort_keys=True, separators=(",", ":"),
    ).encode()
    return hashlib.sha256(serialized).hexdigest()


async def prepare_term(
    session: AsyncSession,
    *,
    org_id: UUID,
    operation_id: UUID,
    actor_id: UUID,
    command: OrgServiceTermCommand,
    head: HistoryHead,
) -> OrgLifecycleOperation:
    """Commit a replayable pending command before touching independent history."""
    digest = _command_digest(org_id, command)
    existing = (await session.execute(
        select(OrgLifecycleOperation).where(OrgLifecycleOperation.id == operation_id)
    )).scalar_one_or_none()
    if existing is not None:
        if (existing.org_id, existing.actor_ops_account_id, existing.command_digest) != (
            org_id, actor_id, digest
        ):
            raise ProblemError(catalog.OPERATION_ID_REUSE)
        if existing.status == "rejected":
            raise ProblemError(catalog.LIFECYCLE_SEQUENCE_STALE)
        return existing

    projection = await identity_service.lifecycle_projection(session, org_id, lock=True)
    if projection is None:
        raise not_found()
    org, current_term, current_policy = projection
    if org.lifecycle_sequence != head.sequence:
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    if command.expected_sequence != org.lifecycle_sequence:
        raise ProblemError(catalog.LIFECYCLE_SEQUENCE_STALE)
    if org.lifecycle_status == "purging":
        raise ProblemError(catalog.PURGE_ALREADY_CLAIMED)
    unresolved = (await session.execute(
        select(OrgLifecycleOperation.id).where(
            OrgLifecycleOperation.org_id == org_id,
            OrgLifecycleOperation.status == "pending",
        )
    )).scalar_one_or_none()
    if unresolved is not None:
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    if org.lifecycle_status == "offboarding":
        if command.current_offboarding_id != org.offboarding_id:
            raise ProblemError(catalog.OFFBOARDING_EPISODE_STALE)
    elif command.current_offboarding_id is not None:
        raise ProblemError(catalog.OFFBOARDING_EPISODE_STALE)
    if (
        current_term is not None and org.lifecycle_status == "active"
        and org.service_ends_at is not None and now() < org.service_ends_at
        and (command.service_start_on != current_term.start_on
             or command.service_last_access_on <= current_term.last_access_on)
    ):
        raise ProblemError(catalog.SERVICE_TERM_INVALID)
    reason = require_reason(command.reason)
    try:
        starts_at, ends_at = service_term_bounds(
            command.service_start_on, command.service_last_access_on, org.timezone
        )
        if command.retention_policy is not None:
            policy = command.retention_policy.model_dump(mode="json")
            if (
                policy["period_unit"] != "elapsed_days"
                and policy["calendar_timezone"] != org.timezone
            ):
                raise ValueError("retention timezone differs from service timezone")
            policy_reference = str(policy["policy_reference"])
            if policy_reference == DEFAULT_POLICY_REFERENCE and (
                policy["period_value"] != 90 or policy["period_unit"] != "elapsed_days"
            ):
                raise ValueError("default retention reference has fixed terms")
            if (
                current_policy is not None
                and policy_reference == current_policy.policy_reference
                and (
                    policy["period_value"] != current_policy.period_value
                    or policy["period_unit"] != current_policy.period_unit
                    or policy["calendar_timezone"] != current_policy.calendar_timezone
                )
            ):
                raise ValueError("changed retention period needs a new policy reference")
        elif current_policy is not None:
            policy = {
                "policy_reference": current_policy.policy_reference,
                "period_value": current_policy.period_value,
                "period_unit": current_policy.period_unit,
                "calendar_timezone": current_policy.calendar_timezone,
            }
            policy_reference = current_policy.policy_reference
        else:
            policy = {
                "policy_reference": DEFAULT_POLICY_REFERENCE,
                "period_value": 90, "period_unit": "elapsed_days",
                "calendar_timezone": None,
            }
            policy_reference = DEFAULT_POLICY_REFERENCE
        deadline = retention_deadline(
            ends_at, int(policy["period_value"]), policy["period_unit"],
            policy["calendar_timezone"],
        )
    except ValueError as exc:
        raise ProblemError(catalog.SERVICE_TERM_INVALID) from exc
    term_id = new_id()
    action = "confirm_term" if current_term is None else "renew_term"
    elapsed_hold = (
        current_term is not None and org.lifecycle_status == "active"
        and org.service_ends_at is not None and now() >= org.service_ends_at
    )
    cancelled_episode = org.offboarding_id
    if elapsed_hold and cancelled_episode is None:
        cancelled_episode = new_id()
    data: dict[str, Any] = {
        "service_term_id": str(term_id), "start_on": command.service_start_on.isoformat(),
        "last_access_on": command.service_last_access_on.isoformat(),
        "starts_at": starts_at.isoformat(), "ends_at": ends_at.isoformat(),
        "calendar_timezone": org.timezone,
        "contract_reference": command.contract_reference,
        "retention_policy": policy, "policy_is_revision": command.retention_policy is not None,
        "projected_purge_eligible_at": deadline.isoformat(),
        "cancelled_offboarding_id": str(cancelled_episode) if cancelled_episode else None,
        "elapsed_hold_started_at": org.service_ends_at.isoformat()
        if elapsed_hold and org.service_ends_at else None,
        "previous_service_end": org.service_ends_at.isoformat()
        if org.service_ends_at else None,
    }
    operation = OrgLifecycleOperation(
        id=operation_id, org_id=org_id, service_term_id=term_id,
        offboarding_id=org.offboarding_id, action=action,
        expected_sequence=command.expected_sequence,
        actor_ops_account_id=actor_id, reason=reason,
        retention_policy_reference=policy_reference,
        requested_deadline=deadline, command_payload=data, command_digest=digest,
    )
    session.add(operation)
    await session.flush()
    return operation


async def apply_term(
    session: AsyncSession, *, operation_id: UUID, event: HistoryEvent,
    replay_by_system: bool = False,
) -> None:
    """Project an accepted decision and its ops audit atomically."""
    operation = (await session.execute(
        select(OrgLifecycleOperation).where(OrgLifecycleOperation.id == operation_id)
        .with_for_update()
    )).scalar_one_or_none()
    if operation is None:
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    if operation.status == "applied":
        return
    if (
        operation.status != "pending" or event.operation_id != operation_id
        or event.org_id != operation.org_id or event.data != operation.command_payload
        or event.sequence != operation.expected_sequence + 1
        or event.actor_id != operation.actor_ops_account_id
        or event.action != operation.action or event.reason != operation.reason
    ):
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    projection = await identity_service.lifecycle_projection(
        session, operation.org_id, lock=True
    )
    if projection is None:
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    org, _, _ = projection
    if org.lifecycle_sequence != operation.expected_sequence:
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    data = event.data
    policy = data["retention_policy"]
    if event.actor_id is None:
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    await identity_service.apply_org_service_term(
        session, org=org, term_id=UUID(data["service_term_id"]),
        sequence=event.sequence, start_on=date.fromisoformat(data["start_on"]),
        last_access_on=date.fromisoformat(data["last_access_on"]),
        starts_at=datetime.fromisoformat(data["starts_at"]),
        ends_at=datetime.fromisoformat(data["ends_at"]),
        contract_reference=data["contract_reference"],
        retention_policy_reference=policy["policy_reference"],
        confirmed_by=event.actor_id, confirmed_at=datetime.fromisoformat(event.accepted_at),
        reason=event.reason,
        custom_policy=policy if data["policy_is_revision"] else None,
    )
    audit_verb = "confirm_org_term" if event.action == "confirm_term" else "renew_org_term"
    audit_ref = {"org_id": str(operation.org_id),
                 "service_term_id": data["service_term_id"]}
    if replay_by_system:
        session.add(OpsAudit(
            id=new_id(), ops_account_id=event.actor_id, verb=audit_verb,
            target_org_id=operation.org_id, target_ref=audit_ref,
            reason=operation.reason,
        ))
    else:
        await append_audit(
            session, verb=audit_verb, target_org_id=operation.org_id,
            target_ref=audit_ref, reason=operation.reason,
        )
    operation.status = "applied"
    operation.resulting_sequence = event.sequence
    operation.resolved_at = now()


async def reject_term(session: AsyncSession, operation_id: UUID) -> None:
    """Release a pending fence only after an authoritative conditional rejection."""
    operation = (await session.execute(
        select(OrgLifecycleOperation).where(OrgLifecycleOperation.id == operation_id)
        .with_for_update()
    )).scalar_one_or_none()
    if operation is not None and operation.status == "pending":
        operation.status = "rejected"
        operation.resolved_at = now()


async def prepare_start_offboarding(
    session: AsyncSession, *, org_id: UUID, operation_id: UUID,
    actor_id: UUID, command: OrgLifecycleCommand, head: HistoryHead,
) -> OrgLifecycleOperation:
    """Fence ordinary work before established calls are drained."""
    digest = _command_digest(org_id, command)
    existing = await session.scalar(select(OrgLifecycleOperation).where(
        OrgLifecycleOperation.id == operation_id,
    ))
    if existing is not None:
        if (existing.org_id != org_id or existing.action != "start"
                or existing.actor_ops_account_id != actor_id
                or existing.command_digest != digest):
            raise ProblemError(catalog.OPERATION_ID_REUSE)
        if existing.status == "rejected":
            raise ProblemError(catalog.LIFECYCLE_SEQUENCE_STALE)
        return existing
    projection = await identity_service.lifecycle_projection(session, org_id, lock=True)
    if projection is None:
        raise not_found()
    org, term, _ = projection
    if org.lifecycle_sequence != head.sequence:
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    if command.expected_sequence != head.sequence:
        raise ProblemError(catalog.LIFECYCLE_SEQUENCE_STALE)
    if org.lifecycle_status == "purging":
        raise ProblemError(catalog.PURGE_ALREADY_CLAIMED)
    if org.lifecycle_status != "active" or term is None or org.service_ends_at is None:
        raise ProblemError(catalog.OFFBOARDING_EPISODE_STALE)
    if now() >= org.service_ends_at:
        raise ProblemError(catalog.SERVICE_TERM_INVALID)
    if await session.scalar(select(OrgLifecycleOperation.id).where(
        OrgLifecycleOperation.org_id == org_id,
        OrgLifecycleOperation.status == "pending",
    )) is not None:
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    period_value, period_unit, calendar_timezone = await _term_policy(session, term)
    episode_id = new_id()
    data: dict[str, Any] = {
        "offboarding_id": str(episode_id), "service_term_id": str(term.id),
        "original_service_end": term.ends_at.isoformat(),
        "retention_policy_reference": term.retention_policy_reference,
        "period_value": period_value, "period_unit": period_unit,
        "calendar_timezone": calendar_timezone,
        "cutoff_at": None, "purge_eligible_at": None,
        "audit_id": str(new_id()),
    }
    operation = OrgLifecycleOperation(
        id=operation_id, org_id=org_id, service_term_id=term.id,
        offboarding_id=episode_id, action="start",
        expected_sequence=head.sequence, actor_ops_account_id=actor_id,
        reason=require_reason(command.reason),
        retention_policy_reference=term.retention_policy_reference,
        command_payload=data, command_digest=digest,
    )
    session.add(operation)
    await session.flush()
    return operation


async def finalize_start_offboarding(
    session: AsyncSession, *, operation_id: UUID,
) -> OrgLifecycleOperation:
    """Fix the accepted cutoff after call quiescence, once per operation."""
    operation = await session.scalar(select(OrgLifecycleOperation).where(
        OrgLifecycleOperation.id == operation_id,
    ).with_for_update())
    if operation is None or operation.action != "start" or operation.status != "pending":
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    if operation.command_payload.get("cutoff_at") is not None:
        return operation
    projection = await identity_service.lifecycle_projection(
        session, operation.org_id, lock=True)
    if projection is None or projection[0].lifecycle_sequence != operation.expected_sequence:
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    org, term, _ = projection
    if (term is None or term.id != operation.service_term_id
            or org.lifecycle_status != "active"):
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    cutoff = now()
    if cutoff >= term.ends_at:
        # The contractual end won while rooms were draining. Retire this
        # unaccepted command so the expiry transition can create its own
        # episode from the persisted service end, without leaving a fence.
        operation.status = "rejected"
        operation.resolved_at = cutoff
        await session.flush()
        return operation
    data = operation.command_payload
    deadline = retention_deadline(
        cutoff, int(data["period_value"]),
        cast(RetentionUnit, data["period_unit"]),
        cast(str | None, data["calendar_timezone"]),
    )
    operation.command_payload = {
        **data, "cutoff_at": cutoff.isoformat(),
        "purge_eligible_at": deadline.isoformat(),
    }
    operation.requested_deadline = deadline
    await session.flush()
    return operation


async def apply_start_offboarding(
    session: AsyncSession, *, operation_id: UUID, event: HistoryEvent,
    replay_by_system: bool = False,
) -> None:
    operation = await session.scalar(select(OrgLifecycleOperation).where(
        OrgLifecycleOperation.id == operation_id,
    ).with_for_update())
    if operation is None or operation.action != "start":
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    if operation.status == "applied":
        return
    if (operation.status != "pending" or event.operation_id != operation_id
            or event.org_id != operation.org_id or event.action != "start"
            or event.actor_id != operation.actor_ops_account_id
            or event.actor_id is None or event.reason != operation.reason
            or event.data != operation.command_payload
            or event.sequence != operation.expected_sequence + 1
            or event.accepted_at != operation.command_payload.get("cutoff_at")):
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    projection = await identity_service.lifecycle_projection(
        session, operation.org_id, lock=True)
    if projection is None:
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    org, term, _ = projection
    if (org.lifecycle_sequence != operation.expected_sequence
            or org.lifecycle_status != "active" or term is None
            or term.id != operation.service_term_id):
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    data = event.data
    cutoff = datetime.fromisoformat(data["cutoff_at"])
    org.lifecycle_status = "offboarding"
    org.lifecycle_sequence = event.sequence
    org.service_ends_at = cutoff
    org.offboarding_id = UUID(data["offboarding_id"])
    org.offboarding_started_at = cutoff
    org.offboarding_started_by = event.actor_id
    org.purge_eligible_at = datetime.fromisoformat(data["purge_eligible_at"])
    org.retention_policy_reference = data["retention_policy_reference"]
    ref = {"org_id": str(org.id), "offboarding_id": data["offboarding_id"],
           "cutoff_at": data["cutoff_at"]}
    if replay_by_system:
        session.add(OpsAudit(
            id=UUID(data["audit_id"]), ops_account_id=event.actor_id,
            verb="start_org_offboarding", target_org_id=org.id,
            target_ref=ref, reason=event.reason, occurred_at=cutoff,
        ))
    else:
        await session.execute(text(
            "select app_append_ops_audit(:id,'start_org_offboarding',"
            ":org,cast(:ref as jsonb),:reason)"
        ), {"id": UUID(data["audit_id"]), "org": org.id,
            "ref": json.dumps(ref), "reason": event.reason})
    operation.status = "applied"
    operation.resulting_sequence = event.sequence
    operation.resolved_at = now()


async def prepare_cancel_offboarding(
    session: AsyncSession, *, org_id: UUID, offboarding_id: UUID,
    operation_id: UUID, actor_id: UUID, command: OrgLifecycleCommand,
    head: HistoryHead,
) -> OrgLifecycleOperation:
    digest = _command_digest(org_id, command)
    existing = await session.scalar(select(OrgLifecycleOperation).where(
        OrgLifecycleOperation.id == operation_id,
    ))
    if existing is not None:
        if (existing.org_id != org_id or existing.action != "cancel"
                or existing.offboarding_id != offboarding_id
                or existing.actor_ops_account_id != actor_id
                or existing.command_digest != digest):
            raise ProblemError(catalog.OPERATION_ID_REUSE)
        if existing.status == "rejected":
            raise ProblemError(catalog.LIFECYCLE_SEQUENCE_STALE)
        return existing
    projection = await identity_service.lifecycle_projection(session, org_id, lock=True)
    if projection is None:
        raise not_found()
    org, term, _ = projection
    if org.lifecycle_sequence != head.sequence:
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    if command.expected_sequence != head.sequence:
        raise ProblemError(catalog.LIFECYCLE_SEQUENCE_STALE)
    if (org.lifecycle_status != "offboarding" or org.offboarding_id != offboarding_id
            or org.offboarding_started_by is None or term is None
            or now() >= term.ends_at):
        raise ProblemError(catalog.OFFBOARDING_EPISODE_STALE)
    if await session.scalar(select(OrgLifecycleOperation.id).where(
        OrgLifecycleOperation.org_id == org_id,
        OrgLifecycleOperation.status == "pending",
    )) is not None:
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    data = {
        "offboarding_id": str(offboarding_id), "service_term_id": str(term.id),
        "restored_service_end": term.ends_at.isoformat(),
        "audit_id": str(new_id()),
    }
    operation = OrgLifecycleOperation(
        id=operation_id, org_id=org_id, service_term_id=term.id,
        offboarding_id=offboarding_id, action="cancel",
        expected_sequence=head.sequence, actor_ops_account_id=actor_id,
        reason=require_reason(command.reason), command_payload=data,
        command_digest=digest,
    )
    session.add(operation)
    await session.flush()
    return operation


async def apply_cancel_offboarding(
    session: AsyncSession, *, operation_id: UUID, event: HistoryEvent,
    replay_by_system: bool = False,
) -> None:
    operation = await session.scalar(select(OrgLifecycleOperation).where(
        OrgLifecycleOperation.id == operation_id,
    ).with_for_update())
    if operation is None or operation.action != "cancel":
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    if operation.status == "applied":
        return
    if (operation.status != "pending" or event.operation_id != operation_id
            or event.org_id != operation.org_id or event.action != "cancel"
            or event.actor_id != operation.actor_ops_account_id
            or event.actor_id is None or event.reason != operation.reason
            or event.data != operation.command_payload
            or event.sequence != operation.expected_sequence + 1):
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    projection = await identity_service.lifecycle_projection(
        session, operation.org_id, lock=True)
    if projection is None:
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    org, term, _ = projection
    if (org.lifecycle_sequence != operation.expected_sequence
            or org.lifecycle_status != "offboarding"
            or org.offboarding_id != operation.offboarding_id
            or org.offboarding_started_by is None or term is None
            or term.id != operation.service_term_id or now() >= term.ends_at):
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    org.lifecycle_status = "active"
    org.lifecycle_sequence = event.sequence
    org.service_ends_at = term.ends_at
    org.offboarding_id = None
    org.offboarding_started_at = None
    org.offboarding_started_by = None
    org.purge_eligible_at = None
    org.retention_policy_reference = None
    ref = {"org_id": str(org.id), "offboarding_id": str(operation.offboarding_id)}
    if replay_by_system:
        session.add(OpsAudit(
            id=UUID(event.data["audit_id"]), ops_account_id=event.actor_id,
            verb="cancel_org_offboarding", target_org_id=org.id,
            target_ref=ref, reason=event.reason,
            occurred_at=datetime.fromisoformat(event.accepted_at),
        ))
    else:
        await session.execute(text(
            "select app_append_ops_audit(:id,'cancel_org_offboarding',"
            ":org,cast(:ref as jsonb),:reason)"
        ), {"id": UUID(event.data["audit_id"]), "org": org.id,
            "ref": json.dumps(ref), "reason": event.reason})
    operation.status = "applied"
    operation.resulting_sequence = event.sequence
    operation.resolved_at = now()


async def prepare_policy_revision(
    session: AsyncSession, *, org_id: UUID, operation_id: UUID,
    actor_id: UUID, command: OrgRetentionPolicyCommand, head: HistoryHead,
) -> OrgLifecycleOperation:
    """Stage a contract revision without rewriting a confirmed service term."""
    data = command.model_dump(mode="json")
    digest = hashlib.sha256(json.dumps(
        {"org_id": str(org_id), "command": data},
        sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    existing = (await session.execute(select(OrgLifecycleOperation).where(
        OrgLifecycleOperation.id == operation_id,
    ))).scalar_one_or_none()
    if existing is not None:
        if (existing.org_id, existing.actor_ops_account_id, existing.command_digest) != (
            org_id, actor_id, digest,
        ) or existing.action != "policy_revision":
            raise ProblemError(catalog.OPERATION_ID_REUSE)
        if existing.status == "rejected":
            raise ProblemError(catalog.LIFECYCLE_SEQUENCE_STALE)
        return existing
    projection = await identity_service.lifecycle_projection(session, org_id, lock=True)
    if projection is None:
        raise not_found()
    org, _, current = projection
    if org.lifecycle_sequence != head.sequence:
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    if command.expected_sequence != head.sequence:
        raise ProblemError(catalog.LIFECYCLE_SEQUENCE_STALE)
    if org.lifecycle_status == "purging":
        raise ProblemError(catalog.PURGE_ALREADY_CLAIMED)
    pending = (await session.execute(select(OrgLifecycleOperation.id).where(
        OrgLifecycleOperation.org_id == org_id,
        OrgLifecycleOperation.status == "pending",
    ))).scalar_one_or_none()
    if pending is not None:
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    if command.period_unit != "elapsed_days" and command.calendar_timezone != org.timezone:
        raise ProblemError(catalog.RETENTION_POLICY_UNVERIFIED)
    if command.policy_reference == DEFAULT_POLICY_REFERENCE:
        raise ProblemError(catalog.RETENTION_POLICY_UNVERIFIED)
    if current is not None and current.policy_reference == command.policy_reference and (
        current.period_value != command.period_value
        or current.period_unit != command.period_unit
        or current.calendar_timezone != command.calendar_timezone
        or current.contract_reference != command.contract_reference
    ):
        raise ProblemError(catalog.RETENTION_POLICY_UNVERIFIED)
    reason = require_reason(command.reason)
    payload = {
        "policy_reference": command.policy_reference,
        "contract_reference": command.contract_reference,
        "period_value": command.period_value,
        "period_unit": command.period_unit,
        "calendar_timezone": command.calendar_timezone,
    }
    operation = OrgLifecycleOperation(
        id=operation_id, org_id=org_id, action="policy_revision",
        expected_sequence=head.sequence, actor_ops_account_id=actor_id,
        reason=reason, retention_policy_reference=command.policy_reference,
        command_payload=payload, command_digest=digest,
    )
    session.add(operation)
    await session.flush()
    return operation


async def apply_policy_revision(
    session: AsyncSession, *, operation_id: UUID, event: HistoryEvent,
    replay_by_system: bool = False,
) -> None:
    """Project one independently accepted revision with its audit record."""
    operation = (await session.execute(select(OrgLifecycleOperation).where(
        OrgLifecycleOperation.id == operation_id,
    ).with_for_update())).scalar_one_or_none()
    if operation is None or operation.action != "policy_revision":
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    if operation.status == "applied":
        return
    if (
        operation.status != "pending" or event.operation_id != operation_id
        or event.org_id != operation.org_id or event.action != operation.action
        or event.actor_id != operation.actor_ops_account_id
        or event.sequence != operation.expected_sequence + 1
        or event.reason != operation.reason or event.data != operation.command_payload
        or event.actor_id is None
    ):
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    projection = await identity_service.lifecycle_projection(session, operation.org_id, lock=True)
    if projection is None or projection[0].lifecycle_sequence != operation.expected_sequence:
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    org, _, current = projection
    values = {
        "policy_reference": event.data["policy_reference"],
        "contract_reference": event.data["contract_reference"],
        "period_value": event.data["period_value"],
        "period_unit": event.data["period_unit"],
        "calendar_timezone": event.data["calendar_timezone"],
        "effective_sequence": event.sequence,
        "approved_at": datetime.fromisoformat(event.accepted_at),
        "approved_by": event.actor_id,
        "reason": event.reason,
    }
    if current is None:
        session.add(OrgRetentionPolicy(org_id=org.id, **values))
    else:
        for key, value in values.items():
            setattr(current, key, value)
    org.lifecycle_sequence = event.sequence
    if replay_by_system:
        session.add(OpsAudit(
            id=new_id(), ops_account_id=event.actor_id,
            verb="revise_org_retention_policy", target_org_id=org.id,
            target_ref={"org_id": str(org.id), "policy_reference": event.data["policy_reference"]},
            reason=event.reason,
        ))
    else:
        await append_audit(
            session, verb="revise_org_retention_policy", target_org_id=org.id,
            target_ref={"org_id": str(org.id), "policy_reference": event.data["policy_reference"]},
            reason=event.reason,
        )
    operation.status = "applied"
    operation.resulting_sequence = event.sequence
    operation.resolved_at = now()


async def prepare_deadline_extension(
    session: AsyncSession, *, org_id: UUID, offboarding_id: UUID,
    operation_id: UUID, actor_id: UUID, command: OrgDeadlineCommand,
    head: HistoryHead,
) -> OrgLifecycleOperation:
    """Stage a strictly later deadline for the named unclaimed episode."""
    digest = hashlib.sha256(json.dumps({
        "org_id": str(org_id), "offboarding_id": str(offboarding_id),
        "command": command.model_dump(mode="json"),
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    existing = (await session.execute(select(OrgLifecycleOperation).where(
        OrgLifecycleOperation.id == operation_id,
    ))).scalar_one_or_none()
    if existing is not None:
        if (
            existing.action != "extend" or existing.org_id != org_id
            or existing.offboarding_id != offboarding_id
            or existing.actor_ops_account_id != actor_id
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
    if org.lifecycle_sequence != head.sequence:
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    if command.expected_sequence != head.sequence:
        raise ProblemError(catalog.LIFECYCLE_SEQUENCE_STALE)
    if org.lifecycle_status == "purging":
        raise ProblemError(catalog.PURGE_ALREADY_CLAIMED)
    if org.lifecycle_status != "offboarding" or org.offboarding_id != offboarding_id:
        raise ProblemError(catalog.OFFBOARDING_EPISODE_STALE)
    pending = (await session.execute(select(OrgLifecycleOperation.id).where(
        OrgLifecycleOperation.org_id == org_id,
        OrgLifecycleOperation.status == "pending",
    ))).scalar_one_or_none()
    if pending is not None:
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    if org.purge_eligible_at is None or command.purge_eligible_at <= org.purge_eligible_at:
        raise ProblemError(catalog.SERVICE_TERM_INVALID)
    reason = require_reason(command.reason)
    data = {
        "offboarding_id": str(offboarding_id),
        "old_deadline": org.purge_eligible_at.isoformat(),
        "new_deadline": command.purge_eligible_at.isoformat(),
    }
    operation = OrgLifecycleOperation(
        id=operation_id, org_id=org_id, offboarding_id=offboarding_id,
        action="extend", expected_sequence=head.sequence,
        actor_ops_account_id=actor_id, reason=reason,
        requested_deadline=command.purge_eligible_at,
        command_payload=data, command_digest=digest,
    )
    session.add(operation)
    await session.flush()
    return operation


async def apply_deadline_extension(
    session: AsyncSession, *, operation_id: UUID, event: HistoryEvent,
    replay_by_system: bool = False,
) -> None:
    """Project an accepted extension and audit it in one transaction."""
    operation = (await session.execute(select(OrgLifecycleOperation).where(
        OrgLifecycleOperation.id == operation_id,
    ).with_for_update())).scalar_one_or_none()
    if operation is None or operation.action != "extend":
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    if operation.status == "applied":
        return
    if (
        operation.status != "pending" or event.operation_id != operation_id
        or event.org_id != operation.org_id or event.action != "extend"
        or event.actor_id != operation.actor_ops_account_id or event.actor_id is None
        or event.reason != operation.reason or event.data != operation.command_payload
        or event.sequence != operation.expected_sequence + 1
    ):
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    projection = await identity_service.lifecycle_projection(session, operation.org_id, lock=True)
    if projection is None:
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    org = projection[0]
    if (
        org.lifecycle_sequence != operation.expected_sequence
        or org.lifecycle_status != "offboarding"
        or org.offboarding_id != operation.offboarding_id
        or org.purge_eligible_at is None
        or org.purge_eligible_at.isoformat() != event.data["old_deadline"]
    ):
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    org.purge_eligible_at = datetime.fromisoformat(event.data["new_deadline"])
    org.lifecycle_sequence = event.sequence
    audit_ref = {
        "org_id": str(org.id), "offboarding_id": str(operation.offboarding_id),
        "old_deadline": event.data["old_deadline"],
        "new_deadline": event.data["new_deadline"],
    }
    if replay_by_system:
        session.add(OpsAudit(
            id=new_id(), ops_account_id=event.actor_id,
            verb="extend_org_deadline", target_org_id=org.id,
            target_ref=audit_ref, reason=event.reason,
        ))
    else:
        await append_audit(
            session, verb="extend_org_deadline", target_org_id=org.id,
            target_ref=audit_ref, reason=event.reason,
        )
    operation.status = "applied"
    operation.resulting_sequence = event.sequence
    operation.resolved_at = now()


async def lifecycle_view(
    session: AsyncSession, org_id: UUID, head: HistoryHead
) -> OrgLifecycleView:
    projection = await identity_service.lifecycle_projection(session, org_id)
    if projection is None:
        raise not_found()
    org, term, _ = projection
    if head.sequence != org.lifecycle_sequence:
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    pending = (await session.execute(
        select(OrgLifecycleOperation).where(
            OrgLifecycleOperation.org_id == org_id,
            OrgLifecycleOperation.status == "pending",
        )
    )).scalar_one_or_none()
    restrictions = (await session.execute(
        select(OrgDeletionRestriction).where(OrgDeletionRestriction.org_id == org_id)
        .order_by(OrgDeletionRestriction.created_at.desc(), OrgDeletionRestriction.id.desc())
    )).scalars().all()
    access_status: Literal["unconfigured", "scheduled", "available", "hold", "purging"]
    if org.lifecycle_status == "purging":
        access_status = "purging"
    elif org.lifecycle_status == "offboarding" or (
        org.service_ends_at is not None and now() >= org.service_ends_at
    ):
        access_status = "hold"
    elif term is None:
        access_status = "unconfigured"
    elif org.service_starts_at is not None and now() < org.service_starts_at:
        access_status = "scheduled"
    else:
        access_status = "available"
    projected: datetime | None = None
    if term is not None:
        period_value, period_unit, calendar_timezone = await _term_policy(session, term)
        projected = retention_deadline(
            term.ends_at, period_value, period_unit, calendar_timezone,
        )
    return OrgLifecycleView(
        org_id=org.id, lifecycle_status=cast(Literal["active", "offboarding", "purging"], org.lifecycle_status),
        access_status=access_status, lifecycle_sequence=org.lifecycle_sequence,
        history_verified=True, service_term_enforced=org.service_term_enforced,
        service_term_id=org.current_service_term_id,
        service_start_on=term.start_on if term else None,
        service_last_access_on=term.last_access_on if term else None,
        service_starts_at=org.service_starts_at, service_ends_at=org.service_ends_at,
        projected_purge_eligible_at=projected,
        service_timezone=term.calendar_timezone if term else None,
        contract_reference=term.contract_reference if term else None,
        pending_operation_id=pending.id if pending else None,
        pending_operation_status=cast(
            Literal["pending", "applied", "rejected"], pending.status
        ) if pending else None,
        offboarding_id=org.offboarding_id,
        offboarding_started_at=org.offboarding_started_at,
        offboarding_started_by=org.offboarding_started_by,
        purge_eligible_at=org.purge_eligible_at,
        retention_policy_reference=org.retention_policy_reference,
        restrictions=[OrgRestrictionView.model_validate({
            "id": item.id, "org_id": item.org_id,
            "offboarding_id": item.offboarding_id, "scope": item.scope,
            "status": item.status, "created_at": item.created_at,
            "released_at": item.released_at,
            "related_request_id": item.related_request_id,
        }) for item in restrictions],
    )


async def retention_policy_view(
    session: AsyncSession, org_id: UUID, head: HistoryHead
) -> OrgRetentionPolicyView:
    projection = await identity_service.lifecycle_projection(session, org_id)
    if projection is None:
        raise not_found()
    org, _, policy = projection
    if head.sequence != org.lifecycle_sequence:
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
    if policy is None:
        return OrgRetentionPolicyView(
            org_id=org.id, source="default",
            policy_reference=DEFAULT_POLICY_REFERENCE,
            period_value=90, period_unit="elapsed_days",
            lifecycle_sequence=org.lifecycle_sequence, history_verified=True,
        )
    return OrgRetentionPolicyView(
        org_id=org.id, source="contract",
        policy_reference=policy.policy_reference,
        contract_reference=policy.contract_reference,
        period_value=policy.period_value,
        period_unit=cast(RetentionUnit, policy.period_unit),
        calendar_timezone=policy.calendar_timezone,
        lifecycle_sequence=org.lifecycle_sequence, history_verified=True,
        approved_at=policy.approved_at,
    )
