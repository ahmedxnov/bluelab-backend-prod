"""Fail-closed established-room and capture quiescence for service suspension."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import aiohttp
from livekit import api
from valkey.asyncio import Valkey

from bluelab.adapters.object_store import DeletionReason, ObjectRef, create_object_store
from bluelab.calls.egress import reconcile_recording
from bluelab.calls.interruption import Disposition, interrupt
from bluelab.calls.lease import CallLease, CallLeaseStore
from bluelab.calls.registry import (
    CallRegistry,
    CallSession,
    CapacitySlots,
    SessionState,
)
from bluelab.calls.scope import lifecycle_scope
from bluelab.platform.config import Settings
from bluelab.platform.db.session import scoped_transaction


class CallQuiescenceUnverified(RuntimeError):
    """Established rooms or capture cannot be proved terminated."""


def _call_id(room_name: str) -> UUID | None:
    if not room_name.startswith("call_"):
        return None
    try:
        return UUID(room_name[5:])
    except ValueError:
        return None


async def _provider_calls(
    client: api.LiveKitAPI, registry: CallRegistry, org_id: UUID,
) -> list[CallSession]:
    rooms = (await client.room.list_rooms(api.ListRoomsRequest())).rooms
    calls: list[CallSession] = []
    for room in rooms:
        call_id = _call_id(room.name)
        if call_id is None:
            continue
        call = await registry.get(call_id)
        if call is None:
            # A lost Valkey entry cannot establish whose live room this is.
            raise CallQuiescenceUnverified("unattributed BlueLab room remains active")
        if call.org_id == org_id:
            calls.append(call)
    return calls


async def _terminate_one(
    client: api.LiveKitAPI, registry: CallRegistry, valkey: Valkey,
    settings: Settings, call: CallSession,
) -> None:
    room_name = f"call_{call.call_id}"
    if settings.livekit_token_revocation_supported:
        # Cloud revocation also covers grants whose room was never established.
        # Pin the cutoff ahead of tokens refreshed during room termination.
        await client.room.remove_participant(api.RoomParticipantIdentity(
            room=room_name,
            identity=call.participant_identity,
            revoke_token_ts=int(datetime.now(UTC).timestamp()) + 30,
        ))
    deadline = asyncio.get_running_loop().time() + settings.dependency_timeout_seconds
    while True:
        active = (await client.egress.list_egress(api.ListEgressRequest(
            room_name=room_name, active=True,
        ))).items
        if not active:
            break
        for entry in active:
            await client.egress.stop_egress(api.StopEgressRequest(egress_id=entry.egress_id))
        if asyncio.get_running_loop().time() >= deadline:
            raise CallQuiescenceUnverified("room capture did not stop")
        await asyncio.sleep(0.2)
    rooms = (await client.room.list_rooms(api.ListRoomsRequest(names=[room_name]))).rooms
    if rooms:
        await client.room.delete_room(api.DeleteRoomRequest(room=room_name))
    if call.attempt_id is not None:
        disposition = (
            Disposition.NEVER_ESTABLISHED
            if call.state is SessionState.PENDING else Disposition.INSTANCE_LOST
        )
        async with scoped_transaction(lifecycle_scope(call)) as db:
            await interrupt(
                db, attempt_id=call.attempt_id, drill_id=call.drill_id,
                rep_account_id=None if call.participant_kind == "candidate"
                else call.participant_id,
                disposition=disposition,
                candidate_id=call.participant_id
                if call.participant_kind == "candidate" else None,
                org_id=call.org_id, team_id=call.team_id,
            )
            if disposition is not Disposition.NEVER_ESTABLISHED:
                await reconcile_recording(db, call, available=False)
        ref = ObjectRef.recording(org_id=call.org_id, attempt_id=call.attempt_id)
        object_store = create_object_store(settings)
        await object_store.delete(ref, reason=DeletionReason.SWEEP)
        if await object_store.exists(ref):
            raise CallQuiescenceUnverified("partial recording object remains")
    lease = CallLease.pending(
        participant_kind=call.participant_kind, participant_id=call.participant_id,
        call_id=call.call_id, attempt_id=call.attempt_id,
    )
    await CallLeaseStore(valkey, ttl_seconds=settings.call_lease_seconds).release(lease)
    await CapacitySlots(valkey, capacity=settings.call_capacity).release(
        call.call_id, call.capacity_slot,
    )
    await registry.mark_terminal(call)


async def quiesce_org_calls(
    org_id: UUID, *, since: datetime, valkey: Valkey, settings: Settings,
) -> None:
    """Drain rooms and wait out existing join grants before a decision is accepted."""
    if (not settings.livekit_url or settings.livekit_api_key is None
            or settings.livekit_api_secret is None):
        raise CallQuiescenceUnverified("live call authority unavailable")
    registry = CallRegistry(valkey)
    client = api.LiveKitAPI(
        settings.livekit_url,
        settings.livekit_api_key.get_secret_value(),
        settings.livekit_api_secret.get_secret_value(),
        timeout=aiohttp.ClientTimeout(total=settings.dependency_timeout_seconds),
    )
    try:
        before = await _provider_calls(client, registry, org_id)
        known = {call.call_id: call for call in await registry.active_for_org(org_id)}
        for call in before:
            known[call.call_id] = call
        for call in known.values():
            await _terminate_one(client, registry, valkey, settings, call)
        grant_deadline = since.astimezone(UTC) + timedelta(seconds=60)
        remaining = (grant_deadline - datetime.now(UTC)).total_seconds()
        if remaining > 0:
            await asyncio.sleep(remaining)
        if await _provider_calls(client, registry, org_id):
            raise CallQuiescenceUnverified("room reconnected after suspension")
        if await registry.active_for_org(org_id):
            raise CallQuiescenceUnverified("active call registry entries remain")
        if not settings.livekit_token_revocation_supported:
            raise CallQuiescenceUnverified("provider token revocation unavailable")
    except CallQuiescenceUnverified:
        raise
    except Exception as exc:
        raise CallQuiescenceUnverified("live call quiescence unavailable") from exc
    finally:
        await client.aclose()
