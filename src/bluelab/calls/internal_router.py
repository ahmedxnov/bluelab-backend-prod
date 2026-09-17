"""The three signed internal endpoints (api/02 §1.2, ADR-0071 rule 2):

    GET  /internal/calls/{call_id}/bundle       -> bundle build
    POST /internal/calls/{call_id}/completion   -> T-2, one transaction
    POST /internal/calls/{call_id}/interruption -> T-6

All authenticated with `X-Agent-Timestamp` and `X-Agent-Signature`: HMAC-SHA256 binds method,
call-specific path, timestamp, and the exact raw-body digest, verified constant-time and failing
closed. This surface is not part of
`api/openapi.yaml` — it is the plane-to-plane seam, not a product surface.

The independently deployed agent uses these exact call-keyed paths and buffers transcript segments
until the completion callback. The backend route implementations remain normal feature work; this
module may not introduce alternate compatibility paths for the retired seam.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text

from bluelab.adapters.object_store import ObjectRef
from bluelab.calls.bundle_builder import build_bundle
from bluelab.calls.completion import TranscriptRow, complete
from bluelab.calls.deps import get_call_valkey
from bluelab.calls.egress import (
    EgressRegistry,
    delete_orphan_recording,
    reconcile_recording,
    recording_status,
    start_recording,
)
from bluelab.calls.interruption import Disposition, interrupt
from bluelab.calls.lease import CallLease, CallLeaseStore
from bluelab.calls.registry import (
    CallRegistry,
    CallSession,
    CapacitySlots,
    SessionState,
)
from bluelab.calls.scope import lifecycle_scope
from bluelab.platform.config import Settings, get_settings
from bluelab.platform.db.session import scoped_transaction
from bluelab.platform.security.agent_signature import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    verify,
)
from bluelab.platform.telemetry import metrics
from bluelab_runtime_bundle import RuntimeBundle

router = APIRouter(prefix="/internal/calls", include_in_schema=False)
SettingsDep = Annotated[Settings, Depends(get_settings)]


class TranscriptItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sequence_index: int = Field(ge=0)
    speaker: Literal["participant", "buyer"]
    text: str = Field(min_length=1, max_length=20_000)
    timestamp_seconds: float = Field(ge=0, le=900)
    demeanor_label: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def buyer_has_no_demeanor(self) -> TranscriptItem:
        if self.speaker == "buyer" and self.demeanor_label is not None:
            raise ValueError("buyer rows cannot carry a demeanor label")
        return self


class CompletionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contract_version: Literal[1]
    call_id: UUID
    disposition: Literal["completed"]
    duration_seconds: float = Field(ge=0, le=900)
    transcript: list[TranscriptItem]

    @model_validator(mode="after")
    def ordered_once(self) -> CompletionBody:
        indexes = [item.sequence_index for item in self.transcript]
        if indexes != sorted(indexes) or len(indexes) != len(set(indexes)):
            raise ValueError("transcript must be ordered by unique sequence_index")
        return self


class InterruptionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contract_version: Literal[1]
    call_id: UUID
    disposition: Literal["interrupted", "never_established"]
    reason: str = Field(min_length=1, max_length=200)


async def _authenticated_body(request: Request, settings: Settings) -> bytes | None:
    raw = await request.body()
    if not verify(
        settings.agent_hmac_secret.get_secret_value(),
        method=request.method,
        path=request.url.path,
        raw_body=raw,
        presented=request.headers.get(SIGNATURE_HEADER),
        timestamp_header=request.headers.get(TIMESTAMP_HEADER),
        now_epoch=int(time.time()),
    ):
        return None
    return raw


async def _call(request: Request, call_id: UUID) -> CallSession | None:
    return await CallRegistry(get_call_valkey(request)).get(call_id)


async def _finish(request: Request, settings: Settings, call: CallSession) -> None:
    valkey = get_call_valkey(request)
    lease = CallLease.pending(
        participant_kind=call.participant_kind,
        participant_id=call.participant_id,
        call_id=call.call_id,
        attempt_id=call.attempt_id,
    )
    await CallLeaseStore(valkey, ttl_seconds=settings.call_lease_seconds).release(lease)
    await CapacitySlots(valkey, capacity=settings.call_capacity).release(
        call.call_id, call.capacity_slot
    )
    await CallRegistry(valkey).mark_terminal(call)


@router.get("/{call_id}/bundle", response_model=RuntimeBundle)
async def runtime_bundle(
    call_id: UUID, request: Request, settings: SettingsDep
) -> RuntimeBundle | Response:
    if await _authenticated_body(request, settings) is None:
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)
    call = await _call(request, call_id)
    if call is None or call.state is SessionState.TERMINAL:
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    async with scoped_transaction(lifecycle_scope(call)) as db:
        bundle = await build_bundle(db, call)
    if call.state is SessionState.PENDING:
        valkey = get_call_valkey(request)
        registry = CallRegistry(valkey)
        lease = CallLease.pending(
            participant_kind=call.participant_kind,
            participant_id=call.participant_id,
            call_id=call.call_id,
            attempt_id=call.attempt_id,
        )
        lease_established = await CallLeaseStore(
            valkey, ttl_seconds=settings.call_lease_seconds
        ).mark_established(lease)
        if not lease_established:
            return Response(status_code=status.HTTP_404_NOT_FOUND)
        call = await registry.mark_established(call)
        egress = EgressRegistry(valkey)
        if await egress.claim_start(call):
            try:
                egress_id = await start_recording(settings, call)
                if egress_id is not None:
                    await egress.started(call, egress_id)
                else:
                    await egress.release_start(call)
            except Exception:  # noqa: BLE001 -- provider client failures are transport-specific
                await egress.release_start(call)
                metrics.record_call_recovery(outcome="recording_unavailable")
    return bundle


@router.post("/{call_id}/completion", status_code=status.HTTP_204_NO_CONTENT)
async def runtime_completion(
    call_id: UUID, request: Request, settings: SettingsDep
) -> Response:
    raw = await _authenticated_body(request, settings)
    if raw is None:
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)
    try:
        body = CompletionBody.model_validate_json(raw)
    except ValueError:
        return Response(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY)
    if body.call_id != call_id:
        return Response(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY)
    call = await _call(request, call_id)
    if call is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    if call.state is SessionState.TERMINAL:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    if call.attempt_id is not None:
        async with scoped_transaction(lifecycle_scope(call)) as db:
            started_at = (
                await db.execute(
                    text("select started_at from attempt where id=:attempt"),
                    {"attempt": call.attempt_id},
                )
            ).scalar_one_or_none()
            await complete(
                db,
                attempt_id=call.attempt_id,
                org_id=call.org_id,
                team_id=call.team_id,
                rows=[
                    TranscriptRow(
                        seq=item.sequence_index,
                        speaker=item.speaker,
                        at_ms=round(item.timestamp_seconds * 1000),
                        text=item.text,
                        demeanor_label=item.demeanor_label,
                    )
                    for item in body.transcript
                ],
                # Derive both lifecycle timestamps from the attempt's database
                # clock.  This cannot violate ended_at >= started_at even when
                # the API and database hosts have small clock skew.
                ended_at=started_at + timedelta(seconds=round(body.duration_seconds))
                if started_at is not None
                else datetime.now(UTC),
                duration_seconds=round(body.duration_seconds),
                recording_object_key=ObjectRef.recording(
                    org_id=call.org_id, attempt_id=call.attempt_id
                ).key,
                candidate_id=call.participant_id
                if call.participant_kind == "candidate"
                else None,
            )
    await _finish(request, settings, call)
    egress_outcome = await EgressRegistry(get_call_valkey(request)).outcome(call)
    if call.attempt_id is not None and egress_outcome is not None:
        async with scoped_transaction(lifecycle_scope(call)) as db:
            reconciled = await reconcile_recording(
                db, call, available=egress_outcome
            )
            erased = await recording_status(db, call) == "erased"
        if reconciled:
            await CallRegistry(get_call_valkey(request)).recording_resolved(call.call_id)
        elif egress_outcome and erased:
            await delete_orphan_recording(settings, call)
            await CallRegistry(get_call_valkey(request)).recording_resolved(call.call_id)
    metrics.record_call_disposition(
        outcome="completed", duration_seconds=body.duration_seconds
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{call_id}/interruption", status_code=status.HTTP_204_NO_CONTENT)
async def runtime_interruption(
    call_id: UUID, request: Request, settings: SettingsDep
) -> Response:
    raw = await _authenticated_body(request, settings)
    if raw is None:
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)
    try:
        body = InterruptionBody.model_validate_json(raw)
    except ValueError:
        return Response(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY)
    if body.call_id != call_id:
        return Response(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY)
    call = await _call(request, call_id)
    if call is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    if call.state is SessionState.TERMINAL:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    if call.attempt_id is not None:
        disposition = (
            Disposition.NEVER_ESTABLISHED
            if body.disposition == "never_established"
            else Disposition.GRACE_EXCEEDED
        )
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
    await _finish(request, settings, call)
    metrics.record_call_disposition(outcome=body.disposition)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
