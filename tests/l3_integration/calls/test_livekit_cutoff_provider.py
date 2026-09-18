"""Optional provider-level room termination against a local LiveKit server."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import fakeredis.aioredis
import pytest
from livekit import api

from bluelab.calls.registry import CallRegistry, CallSession, SessionState
from bluelab.calls.suspension import CallQuiescenceUnverified, quiesce_org_calls
from bluelab.platform.config import Settings

pytestmark = [pytest.mark.l3_integration, pytest.mark.asyncio]


def _provider_settings() -> Settings:
    url = os.getenv("LIVEKIT_PROVIDER_TEST_URL")
    if not url:
        pytest.skip("LIVEKIT_PROVIDER_TEST_URL selects a disposable LiveKit project")
    cloud = os.getenv("LIVEKIT_PROVIDER_TEST_CLOUD_REVOCATION") == "true"
    key = os.getenv("LIVEKIT_PROVIDER_TEST_KEY")
    secret = os.getenv("LIVEKIT_PROVIDER_TEST_SECRET")
    if cloud and (not key or not secret):
        pytest.fail("Cloud provider test requires LIVEKIT_PROVIDER_TEST_KEY and SECRET")
    return Settings(
        BLUELAB_ENV="staging",
        DATABASE_URL="postgresql+asyncpg://test:test@localhost/test",  # pragma: allowlist secret
        VALKEY_URL="redis://localhost:6379/0",
        AGENT_HMAC_SECRET="test-secret-not-real",  # pragma: allowlist secret
        LIVEKIT_URL=url,
        LIVEKIT_API_KEY=key or "devkey",
        LIVEKIT_API_SECRET=secret or "secret",
        LIVEKIT_TOKEN_REVOCATION_SUPPORTED=cloud,
    )


@pytest.mark.asyncio
async def test_real_provider_room_terminates_and_unknown_room_fails_closed() -> None:
    settings = _provider_settings()
    org_id, call_id, person_id, drill_id = (uuid4() for _ in range(4))
    room_name = f"call_{call_id}"
    client = api.LiveKitAPI(
        settings.livekit_url,
        settings.livekit_api_key.get_secret_value(),
        settings.livekit_api_secret.get_secret_value(),
    )
    valkey = fakeredis.aioredis.FakeRedis(decode_responses=True)
    registry = CallRegistry(valkey)
    try:
        await client.room.create_room(api.CreateRoomRequest(name=room_name))
        await registry.put(CallSession(
            call_id=call_id, mode="test", participant_kind="author",
            participant_id=person_id, participant_identity=f"acct_{person_id}",
            participant_display_name="Provider test", org_id=org_id,
            team_id=org_id, drill_id=drill_id, attempt_id=None,
            state=SessionState.ESTABLISHED,
        ))
        if settings.livekit_token_revocation_supported:
            await quiesce_org_calls(
                org_id, since=datetime.now(UTC) - timedelta(seconds=65),
                valkey=valkey, settings=settings,
            )
        else:
            with pytest.raises(CallQuiescenceUnverified, match="token revocation unavailable"):
                await quiesce_org_calls(
                    org_id, since=datetime.now(UTC) - timedelta(seconds=65),
                    valkey=valkey, settings=settings,
                )
        assert (await registry.get(call_id)).state is SessionState.TERMINAL
        assert not (await client.room.list_rooms(api.ListRoomsRequest(
            names=[room_name],
        ))).rooms

        # Provider truth takes precedence over lost coordination state.
        await client.room.create_room(api.CreateRoomRequest(name=room_name))
        await registry.delete(call_id)
        with pytest.raises(CallQuiescenceUnverified):
            await quiesce_org_calls(
                org_id, since=datetime.now(UTC) - timedelta(seconds=65),
                valkey=valkey, settings=settings,
            )
    finally:
        try:
            remaining = (await client.room.list_rooms(api.ListRoomsRequest(
                names=[room_name],
            ))).rooms
            if remaining:
                await client.room.delete_room(api.DeleteRoomRequest(room=room_name))
        finally:
            await client.aclose()
            await valkey.aclose()

