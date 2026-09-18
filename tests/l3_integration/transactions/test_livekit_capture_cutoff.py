"""Optional real-provider cutoff of egress, attempt, and exact recording key."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta

import fakeredis.aioredis
import pytest
from livekit import api
from sqlalchemy import text

from bluelab.adapters.object_store import DeletionReason, ObjectRef, create_object_store
from bluelab.calls.egress import start_recording
from bluelab.calls.registry import CallRegistry, CallSession, SessionState
from bluelab.calls.suspension import CallQuiescenceUnverified, quiesce_org_calls
from bluelab.platform.config import Settings
from bluelab.platform.ids import new_id


@pytest.mark.asyncio
async def test_livekit_cutoff_reconciles_capture(
    session, base_org, make_drill,
) -> None:
    provider = os.getenv("LIVEKIT_PROVIDER_TEST_URL")
    storage = os.getenv("LIVEKIT_PROVIDER_TEST_S3_ENDPOINT")
    if not provider or not storage:
        if os.getenv("LIVEKIT_PROVIDER_TEST_CLOUD_REVOCATION") == "true":
            pytest.fail("Cloud capture test requires LiveKit and disposable S3 configuration")
        pytest.skip("disposable LiveKit, egress, and S3 services are required")
    cloud = os.getenv("LIVEKIT_PROVIDER_TEST_CLOUD_REVOCATION") == "true"
    provider_key = os.getenv("LIVEKIT_PROVIDER_TEST_KEY", "devkey")
    provider_secret = os.getenv("LIVEKIT_PROVIDER_TEST_SECRET", "secret")  # pragma: allowlist secret
    bucket = os.getenv("LIVEKIT_PROVIDER_TEST_S3_BUCKET", "bluelab-test")
    region = os.getenv("LIVEKIT_PROVIDER_TEST_S3_REGION", "us-east-1")
    access_key = os.getenv("LIVEKIT_PROVIDER_TEST_S3_ACCESS_KEY", "bluelabtest")
    secret_key = os.getenv("LIVEKIT_PROVIDER_TEST_S3_SECRET_KEY", "bluelabtestsecret")  # pragma: allowlist secret
    if cloud and not all(os.getenv(name) for name in (
        "LIVEKIT_PROVIDER_TEST_KEY", "LIVEKIT_PROVIDER_TEST_SECRET",
        "LIVEKIT_PROVIDER_TEST_S3_BUCKET", "LIVEKIT_PROVIDER_TEST_S3_REGION",
        "LIVEKIT_PROVIDER_TEST_S3_ACCESS_KEY", "LIVEKIT_PROVIDER_TEST_S3_SECRET_KEY",
    )):
        pytest.fail("Cloud capture test requires provider and disposable S3 credentials")
    drill = await make_drill(status="published", self_authored=True)
    call_id, attempt_id = new_id(), new_id()
    room_name = f"call_{call_id}"
    ref = ObjectRef.recording(org_id=base_org["org"], attempt_id=attempt_id)
    settings = Settings(
        BLUELAB_ENV="staging", DATABASE_URL=os.environ["TEST_DATABASE_URL"],
        VALKEY_URL="redis://127.0.0.1:6379/0", AGENT_HMAC_SECRET="test-secret-not-real",  # pragma: allowlist secret
        LIVEKIT_URL=provider, LIVEKIT_API_KEY=provider_key,
        LIVEKIT_API_SECRET=provider_secret,
        LIVEKIT_TOKEN_REVOCATION_SUPPORTED=cloud,
        OBJECT_STORE_ENDPOINT=storage,
        OBJECT_STORE_BUCKET=bucket, OBJECT_STORE_REGION=region,
        OBJECT_STORE_ACCESS_KEY=access_key,
        OBJECT_STORE_SECRET_KEY=secret_key,
    )
    objects = create_object_store(settings)
    valkey = fakeredis.aioredis.FakeRedis(decode_responses=True)
    registry = CallRegistry(valkey)
    provider_client = api.LiveKitAPI(provider, provider_key, provider_secret)
    call = CallSession(
        call_id=call_id, mode="attempt", participant_kind="rep",
        participant_id=base_org["manager"],
        participant_identity=f"acct_{base_org['manager']}",
        participant_display_name="Capture test", org_id=base_org["org"],
        team_id=base_org["manager"], drill_id=drill,
        attempt_id=attempt_id, state=SessionState.ESTABLISHED,
    )
    try:
        async with session.begin():
            await session.execute(text(
                "insert into attempt(id,org_id,team_id,drill_id,rep_account_id,"
                "self_authored,status,recording_object_key,recording_status) "
                "values(:id,:org,:team,:drill,:rep,true,'in_progress',:key,'pending')"
            ), {
                "id": attempt_id, "org": base_org["org"],
                "team": base_org["manager"], "drill": drill,
                "rep": base_org["manager"], "key": ref.key,
            })
        await objects.put(ref, b"partial capture", content_type="audio/ogg")
        await provider_client.room.create_room(api.CreateRoomRequest(name=room_name))
        assert await start_recording(settings, call)
        assert (await provider_client.egress.list_egress(api.ListEgressRequest(
            room_name=room_name, active=True,
        ))).items
        await registry.put(call)
        if cloud:
            await quiesce_org_calls(
                base_org["org"], since=datetime.now(UTC) - timedelta(seconds=65),
                valkey=valkey, settings=settings,
            )
        else:
            with pytest.raises(CallQuiescenceUnverified, match="token revocation unavailable"):
                await quiesce_org_calls(
                    base_org["org"], since=datetime.now(UTC) - timedelta(seconds=65),
                    valkey=valkey, settings=settings,
                )
        await asyncio.sleep(1)
        assert not (await provider_client.egress.list_egress(api.ListEgressRequest(
            room_name=room_name, active=True,
        ))).items
        assert not await objects.exists(ref)
        async with session.begin():
            outcome = (await session.execute(text(
                "select status, recording_status from attempt where id=:id"
            ), {"id": attempt_id})).one()
            assert tuple(outcome) == ("interrupted", "unavailable")
    finally:
        try:
            active = (await provider_client.egress.list_egress(api.ListEgressRequest(
                room_name=room_name, active=True,
            ))).items
            for entry in active:
                await provider_client.egress.stop_egress(api.StopEgressRequest(
                    egress_id=entry.egress_id,
                ))
            remaining = (await provider_client.room.list_rooms(api.ListRoomsRequest(
                names=[room_name],
            ))).rooms
            if remaining:
                await provider_client.room.delete_room(api.DeleteRoomRequest(room=room_name))
        finally:
            await provider_client.aclose()
            await valkey.aclose()
            await objects.delete(ref, reason=DeletionReason.SWEEP)
            async with session.begin():
                await session.execute(text("delete from ops_fault where attempt_id=:id"), {"id": attempt_id})
                await session.execute(text("delete from attempt where id=:id"), {"id": attempt_id})
