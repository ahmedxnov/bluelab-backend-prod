"""Provider-authenticated LiveKit reconciliation webhook."""

from __future__ import annotations

import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from livekit import api

from bluelab.calls.deps import get_call_valkey
from bluelab.calls.egress import (
    EgressRegistry,
    attempt_status,
    delete_orphan_recording,
    reconcile_recording,
    recording_status,
)
from bluelab.calls.interruption import Disposition, interrupt
from bluelab.calls.lease import CallLeaseStore
from bluelab.calls.registry import CallRegistry, CapacitySlots, SessionState
from bluelab.calls.scope import lifecycle_scope
from bluelab.platform.config import Settings, get_settings
from bluelab.platform.db.session import scoped_transaction
from bluelab.platform.errors import catalog
from bluelab.platform.errors.denial import ProblemError
from bluelab.platform.telemetry import metrics

router = APIRouter(tags=["Hooks"])
SettingsDep = Annotated[Settings, Depends(get_settings)]


def _call_id(room_name: str) -> UUID | None:
    if not room_name.startswith("call_"):
        return None
    try:
        return UUID(room_name[5:])
    except ValueError:
        return None


@router.post(
    "/hooks/livekit",
    operation_id="receiveLivekitWebhook",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def livekit_webhook(request: Request, settings: SettingsDep) -> Response:
    if settings.livekit_api_key is None or settings.livekit_api_secret is None:
        raise ProblemError(catalog.WEBHOOK_SIGNATURE_INVALID)
    raw = await request.body()
    authorization = request.headers.get("Authorization", "")
    token = (
        authorization[7:].strip()
        if authorization.startswith("Bearer ")
        else authorization.strip()
    )
    try:
        event = api.WebhookReceiver(
            api.TokenVerifier(
                settings.livekit_api_key.get_secret_value(),
                settings.livekit_api_secret.get_secret_value(),
            )
        ).receive(raw.decode("utf-8"), token)
    except Exception:  # noqa: BLE001 -- digest mismatch is raised as plain Exception
        raise ProblemError(catalog.WEBHOOK_SIGNATURE_INVALID) from None
    payload = json.loads(raw)
    room_name = str(
        (payload.get("room") or {}).get("name") or payload.get("roomName") or ""
    )
    call_id = _call_id(room_name)
    if call_id is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    valkey = get_call_valkey(request)
    registry = CallRegistry(valkey)
    call = await registry.get(call_id)
    if call is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    if event.event == "egress_ended":
        egress_info = payload.get("egressInfo") or {}
        state = str(egress_info.get("status") or "").upper()
        if state in {"EGRESS_COMPLETE", "EGRESS_FAILED", "EGRESS_ABORTED"}:
            egress_id = str(egress_info.get("egressId") or "")
            egress = EgressRegistry(valkey)
            if not await egress.matches(call, egress_id):
                return Response(status_code=status.HTTP_204_NO_CONTENT)
            available = state == "EGRESS_COMPLETE"
            await egress.set_outcome(call, available=available)
            async with scoped_transaction(lifecycle_scope(call)) as db:
                reconciled = await reconcile_recording(db, call, available=available)
                interrupted = await attempt_status(db, call) == "interrupted"
                erased = await recording_status(db, call) == "erased"
            if reconciled:
                await registry.recording_resolved(call.call_id)
            elif available and (interrupted or erased):
                await delete_orphan_recording(settings, call)
                await registry.recording_resolved(call.call_id)
    elif event.event in {"room_finished", "participant_connection_aborted"}:
        lease = await CallLeaseStore(
            valkey, ttl_seconds=settings.call_lease_seconds
        ).current(call.participant_kind, call.participant_id)
        if lease is None and call.attempt_id is not None:
            disposition = (
                Disposition.NEVER_ESTABLISHED
                if call.state is SessionState.PENDING
                else Disposition.INSTANCE_LOST
            )
            async with scoped_transaction(lifecycle_scope(call)) as db:
                changed = await interrupt(
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
            if changed:
                await CapacitySlots(
                    valkey, capacity=settings.call_capacity
                ).release(call.call_id, call.capacity_slot)
                await registry.mark_terminal(call)
                outcome = (
                    "never_established"
                    if disposition is Disposition.NEVER_ESTABLISHED
                    else "interrupted"
                )
                metrics.record_call_disposition(outcome=outcome)
                metrics.record_call_recovery(outcome="lease_expired")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
