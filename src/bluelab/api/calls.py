"""Call admission and pending-call cancellation product routes."""

from __future__ import annotations

import time
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.api.deps import CallPrincipal, get_valkey, scope_of
from bluelab.calls.admission import (
    AdmissionRequest,
    ParticipantKind,
    admit,
    has_current_consent,
)
from bluelab.calls.interruption import Disposition, interrupt
from bluelab.calls.lease import CallLease, CallLeaseStore
from bluelab.calls.placement import mint_participant_token
from bluelab.calls.registry import (
    CallRegistry,
    CallSession,
    CapacitySlots,
    SessionState,
)
from bluelab.calls.scope import lifecycle_scope
from bluelab.modules.identity.gates import KIND_NOTICE, current_versions
from bluelab.platform.config import Settings, get_settings
from bluelab.platform.db.scope import ScopeContext
from bluelab.platform.db.session import scoped_transaction
from bluelab.platform.errors import catalog
from bluelab.platform.errors.denial import ProblemError, not_found
from bluelab.platform.ids import new_id
from bluelab.platform.security.sessions import SessionRecord
from bluelab.platform.security.tokens import CandidateBinding
from bluelab.platform.telemetry import metrics

router = APIRouter(tags=["Calls"])
SettingsDep = Annotated[Settings, Depends(get_settings)]


class CallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    drill_id: UUID
    mode: Literal["attempt", "test"] = "attempt"
    stage_id: UUID | None = None


class StagePosition(BaseModel):
    ord: int
    of: int


class LiveKitGrant(BaseModel):
    url: str
    room: str
    token: str
    join_deadline_seconds: Literal[60] = 60


class CallGrant(BaseModel):
    call_id: UUID
    mode: Literal["attempt", "test"]
    attempt_id: UUID | None
    stage: StagePosition | None
    livekit: LiveKitGrant


def _scope(principal: SessionRecord | CandidateBinding) -> ScopeContext:
    if isinstance(principal, CandidateBinding):
        return ScopeContext.candidate(
            org_id=principal.org_id,
            team_id=principal.team_id,
            position_id=principal.position_id,
            candidate_id=principal.candidate_id,
        )
    return scope_of(principal)


async def _account_context(
    db: AsyncSession, principal: SessionRecord, payload: CallRequest
) -> tuple[AdmissionRequest, str, int | None, int | None]:
    row = (
        (
            await db.execute(
                text("""
                select d.id,d.self_authored,d.author_account_id,d.status,a.display_name,
                       exists(select 1 from assignment x join assignment_recipient ar on ar.assignment_id=x.id
                              where x.drill_id=d.id and ar.rep_account_id=:account) assigned
                  from drill d join account a on a.id=:account
                 where d.id=:drill and d.org_id=:org and d.team_id=:team
            """),
                {
                    "account": UUID(principal.account_id),
                    "drill": payload.drill_id,
                    "org": UUID(principal.org_id),
                    "team": UUID(principal.team_id),
                },
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise not_found()
    is_author = row["author_account_id"] == UUID(principal.account_id)
    if row["status"] != "published":
        raise ProblemError(catalog.DRILL_NOT_STARTABLE)
    if payload.stage_id is not None:
        raise ProblemError(catalog.DRILL_NOT_STARTABLE)
    if payload.mode == "test":
        if not (is_author or principal.role == "manager"):
            raise not_found()
        kind = ParticipantKind.AUTHOR
    else:
        if principal.role != "rep" or not (
            bool(row["self_authored"]) and is_author or bool(row["assigned"])
        ):
            raise ProblemError(catalog.DRILL_NOT_STARTABLE)
        kind = ParticipantKind.REP
    return (
        AdmissionRequest(
            kind=kind,
            org_id=UUID(principal.org_id),
            team_id=UUID(principal.team_id),
            drill_id=payload.drill_id,
            account_id=UUID(principal.account_id),
            self_authored=bool(row["self_authored"]),
        ),
        str(row["display_name"]),
        None,
        None,
    )


async def _candidate_context(
    db: AsyncSession, principal: CandidateBinding, payload: CallRequest
) -> tuple[AdmissionRequest, str, int, int]:
    if payload.mode != "attempt" or payload.stage_id is None:
        raise ProblemError(catalog.DRILL_NOT_STARTABLE)
    row = (
        (
            await db.execute(
                text("""
                select c.name,c.preflight,c.completed_at,s.ord,
                       (select count(*)::int from assessment_stage x where x.position_id=c.position_id) stage_total
                  from candidate c join assessment_stage s on s.position_id=c.position_id
                 where c.id=:candidate and s.id=:stage and s.drill_id=:drill
            """),
                {
                    "candidate": principal.candidate_id,
                    "stage": payload.stage_id,
                    "drill": payload.drill_id,
                },
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise not_found()
    if row["completed_at"] is not None:
        raise ProblemError(catalog.ASSESSMENT_COMPLETED)
    if not bool((row["preflight"] or {}).get("passed")):
        raise ProblemError(catalog.PREFLIGHT_REQUIRED)
    return (
        AdmissionRequest(
            kind=ParticipantKind.CANDIDATE,
            org_id=principal.org_id,
            team_id=principal.team_id,
            drill_id=payload.drill_id,
            candidate_id=principal.candidate_id,
            assessment_stage_id=payload.stage_id,
        ),
        str(row["name"]),
        int(row["ord"]),
        int(row["stage_total"]),
    )


@router.post(
    "/calls",
    operation_id="requestCall",
    response_model=CallGrant,
    status_code=status.HTTP_201_CREATED,
)
async def request_call(
    payload: CallRequest,
    principal: CallPrincipal,
    request: Request,
    settings: SettingsDep,
) -> CallGrant:
    started = time.perf_counter()
    valkey = get_valkey(request)
    participant_id = (
        principal.candidate_id
        if isinstance(principal, CandidateBinding)
        else UUID(principal.account_id)
    )
    participant_kind: Literal["rep", "candidate", "author"] = (
        "candidate"
        if isinstance(principal, CandidateBinding)
        else ("author" if payload.mode == "test" else "rep")
    )
    call_id = new_id()
    lease = CallLease.pending(
        participant_kind=participant_kind,
        participant_id=participant_id,
        call_id=call_id,
        attempt_id=None,
    )
    leases = CallLeaseStore(valkey, ttl_seconds=settings.call_lease_seconds)
    slots = CapacitySlots(valkey, capacity=settings.call_capacity)
    registry = CallRegistry(valkey)
    slot: int | None = None
    outcome = "failed"
    if not await leases.acquire(lease):
        metrics.record_admission(
            duration_ms=(time.perf_counter() - started) * 1000, outcome="already_active"
        )
        raise ProblemError(catalog.CALL_ALREADY_ACTIVE)
    try:
        slot = await slots.acquire(call_id)
        if slot is None or await slots.spend_blocked():
            outcome = "capacity"
            raise ProblemError(
                catalog.CALL_CAPACITY,
                headers={"Retry-After": str(settings.call_capacity_retry_seconds)},
            )
        if (
            not settings.livekit_url
            or settings.livekit_api_key is None
            or settings.livekit_api_secret is None
        ):
            outcome = "capability"
            raise ProblemError(
                catalog.CALL_CAPACITY,
                headers={"Retry-After": str(settings.call_capacity_retry_seconds)},
            )

        async with scoped_transaction(_scope(principal)) as db:
            versions = await current_versions(db, KIND_NOTICE)
            consent_version = versions.get(KIND_NOTICE)
            if consent_version is None:
                raise ProblemError(catalog.CONSENT_REQUIRED)
            context, display_name, stage_ord, stage_total = (
                await _candidate_context(db, principal, payload)
                if isinstance(principal, CandidateBinding)
                else await _account_context(db, principal, payload)
            )
            if payload.mode == "test" and not await has_current_consent(
                db,
                account_id=context.account_id,
                candidate_id=context.candidate_id,
                consent_version=consent_version,
            ):
                raise ProblemError(catalog.CONSENT_REQUIRED)
            admitted = (
                None
                if payload.mode == "test"
                else await admit(db, context, consent_version=consent_version)
            )
            attempt_id = admitted.attempt_id if admitted else None
            lease = CallLease.pending(
                participant_kind=participant_kind,
                participant_id=participant_id,
                call_id=call_id,
                attempt_id=attempt_id,
            )
            session = CallSession(
                call_id=call_id,
                mode=payload.mode,
                participant_kind=participant_kind,
                participant_id=participant_id,
                participant_identity=(
                    "cand_" if isinstance(principal, CandidateBinding) else "acct_"
                )
                + str(participant_id),
                participant_display_name=display_name,
                org_id=context.org_id,
                team_id=context.team_id,
                drill_id=payload.drill_id,
                attempt_id=attempt_id,
                stage_id=payload.stage_id,
                stage_ord=stage_ord,
                stage_total=stage_total,
                restart=admitted.restart if admitted else False,
                capacity_slot=slot,
                position_id=principal.position_id
                if isinstance(principal, CandidateBinding)
                else None,
                account_role=None
                if isinstance(principal, CandidateBinding)
                else cast(Literal["manager", "rep"], principal.role),
            )
            await registry.put(session)
            await leases.renew(lease)
            room = f"call_{call_id}"
            token = mint_participant_token(
                api_key=settings.livekit_api_key.get_secret_value(),
                api_secret=settings.livekit_api_secret.get_secret_value(),
                call=session,
            )
        outcome = "placed"
        return CallGrant(
            call_id=call_id,
            mode=payload.mode,
            attempt_id=attempt_id,
            stage=StagePosition(ord=stage_ord, of=stage_total)
            if stage_ord and stage_total
            else None,
            livekit=LiveKitGrant(url=settings.livekit_url, room=room, token=token),
        )
    except ProblemError as exc:
        if outcome == "failed":
            outcome = exc.problem.slug.replace("-", "_")
        await registry.delete(call_id)
        await slots.release(call_id, slot)
        await leases.release(lease)
        raise
    except Exception:
        if outcome != "placed":
            await registry.delete(call_id)
            await slots.release(call_id, slot)
            await leases.release(lease)
        raise
    finally:
        metrics.record_admission(
            duration_ms=(time.perf_counter() - started) * 1000, outcome=outcome
        )


@router.delete(
    "/calls/current",
    operation_id="cancelPendingCall",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def cancel_pending_call(
    principal: CallPrincipal, request: Request, settings: SettingsDep
) -> Response:
    valkey = get_valkey(request)
    participant_id = (
        principal.candidate_id
        if isinstance(principal, CandidateBinding)
        else UUID(principal.account_id)
    )
    # Account test and attempt calls share the same physical key.
    kind: Literal["rep", "candidate", "author"] = (
        "candidate" if isinstance(principal, CandidateBinding) else "rep"
    )
    leases = CallLeaseStore(valkey, ttl_seconds=settings.call_lease_seconds)
    lease = await leases.current(kind, participant_id)
    if lease is None:
        raise ProblemError(catalog.NO_ACTIVE_CALL)
    registry = CallRegistry(valkey)
    session = await registry.get(lease.call_id)
    if session is None:
        await leases.release(lease)
        raise ProblemError(catalog.NO_ACTIVE_CALL)
    if session.state is SessionState.ESTABLISHED:
        raise ProblemError(catalog.CALL_ALREADY_ACTIVE)
    if session.attempt_id is not None:
        async with scoped_transaction(lifecycle_scope(session)) as db:
            await interrupt(
                db,
                attempt_id=session.attempt_id,
                drill_id=session.drill_id,
                rep_account_id=None
                if session.participant_kind == "candidate"
                else session.participant_id,
                disposition=Disposition.NEVER_ESTABLISHED,
            )
    await CapacitySlots(valkey, capacity=settings.call_capacity).release(
        session.call_id, session.capacity_slot
    )
    await leases.release(lease)
    await registry.delete(session.call_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
