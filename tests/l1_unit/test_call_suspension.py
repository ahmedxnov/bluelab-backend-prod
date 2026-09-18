"""A service cutoff is not accepted while a provider room remains live."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import fakeredis.aioredis
import pytest

from bluelab.calls import egress, suspension
from bluelab.calls.egress import start_recording
from bluelab.calls.registry import CallRegistry, CallSession, SessionState
from bluelab.platform.config import Settings

ORG = UUID("01800000-0000-7000-8000-000000000101")
CALL = UUID("01800000-0000-7000-8000-000000000102")
PERSON = UUID("01800000-0000-7000-8000-000000000103")
DRILL = UUID("01800000-0000-7000-8000-000000000104")


def _settings() -> Settings:
    return Settings(
        BLUELAB_ENV="staging",
        DATABASE_URL="postgresql+asyncpg://test:test@localhost/test",  # pragma: allowlist secret
        VALKEY_URL="redis://localhost:6379/0",
        AGENT_HMAC_SECRET="test-secret-not-real",  # pragma: allowlist secret
        LIVEKIT_URL="wss://calls.example.test",
        LIVEKIT_API_KEY="key",  # pragma: allowlist secret
        LIVEKIT_API_SECRET="secret",  # pragma: allowlist secret
        LIVEKIT_TOKEN_REVOCATION_SUPPORTED=True,
    )


class _Provider:
    def __init__(self, *, room: str | None, fail_revocation: bool = False) -> None:
        self.room_name = room
        self.fail_revocation = fail_revocation
        self.deleted: list[str] = []
        self.revoked: list[tuple[str, str, int]] = []
        self.room = SimpleNamespace(
            list_rooms=self.list_rooms,
            delete_room=self.delete_room,
            remove_participant=self.remove_participant,
        )
        self.egress = SimpleNamespace(list_egress=self.list_egress)

    async def list_rooms(self, request):
        names = set(request.names)
        present = self.room_name and (not names or self.room_name in names)
        return SimpleNamespace(rooms=[SimpleNamespace(name=self.room_name)] if present else [])

    async def delete_room(self, request):
        assert self.revoked
        self.deleted.append(request.room)
        self.room_name = None

    async def remove_participant(self, request):
        if self.fail_revocation:
            raise RuntimeError("provider revocation failed")
        self.revoked.append((request.room, request.identity, request.revoke_token_ts))

    async def list_egress(self, request):
        return SimpleNamespace(items=[])

    async def aclose(self) -> None:
        pass


@pytest.mark.asyncio
async def test_test_call_never_starts_recording(monkeypatch) -> None:
    def unexpected_provider(*args, **kwargs):
        raise AssertionError("test calls must not start LiveKit egress")

    monkeypatch.setattr(suspension.api, "LiveKitAPI", unexpected_provider)
    call = CallSession(
        call_id=CALL, mode="test", participant_kind="author", participant_id=PERSON,
        participant_identity="author", participant_display_name="Test",
        org_id=ORG, team_id=ORG, drill_id=DRILL,
        attempt_id=None, state=SessionState.ESTABLISHED,
    )
    assert await start_recording(_settings(), call) is None


@pytest.mark.asyncio
async def test_attempt_recording_targets_authoritative_key(monkeypatch) -> None:
    requests = []

    async def start(request):
        requests.append(request)
        return SimpleNamespace(egress_id="egress-id")

    async def close() -> None:
        pass

    provider = SimpleNamespace(
        egress=SimpleNamespace(start_room_composite_egress=start),
        aclose=close,
    )
    monkeypatch.setattr(egress.api, "LiveKitAPI", lambda *args, **kwargs: provider)
    call = CallSession(
        call_id=CALL, mode="attempt", participant_kind="rep", participant_id=PERSON,
        participant_identity="acct_" + str(PERSON), participant_display_name="Rep",
        org_id=ORG, team_id=ORG, drill_id=DRILL,
        attempt_id=CALL, state=SessionState.ESTABLISHED,
    )
    assert await start_recording(_settings(), call) == "egress-id"
    assert len(requests) == 1
    assert requests[0].room_name == f"call_{CALL}"
    assert requests[0].audio_only is True
    assert requests[0].file_outputs[0].filepath == (
        f"orgs/{ORG}/recordings/{CALL}.ogg"
    )


@pytest.mark.asyncio
async def test_unknown_provider_room_blocks_service_decision(monkeypatch) -> None:
    provider = _Provider(room=f"call_{CALL}")
    monkeypatch.setattr(suspension.api, "LiveKitAPI", lambda *args, **kwargs: provider)
    valkey = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        with pytest.raises(suspension.CallQuiescenceUnverified):
            await suspension.quiesce_org_calls(
                ORG, since=datetime.now(UTC) - timedelta(minutes=2),
                valkey=valkey, settings=_settings(),
            )
        assert provider.deleted == []
    finally:
        await valkey.aclose()


@pytest.mark.asyncio
async def test_pending_grant_is_revoked_without_a_room(monkeypatch) -> None:
    provider = _Provider(room=None)
    monkeypatch.setattr(suspension.api, "LiveKitAPI", lambda *args, **kwargs: provider)
    valkey = fakeredis.aioredis.FakeRedis(decode_responses=True)
    registry = CallRegistry(valkey)
    call = CallSession(
        call_id=CALL, mode="test", participant_kind="author", participant_id=PERSON,
        participant_identity="author", participant_display_name="Test",
        org_id=ORG, team_id=ORG, drill_id=DRILL,
        attempt_id=None, state=SessionState.PENDING,
    )
    try:
        await registry.put(call)
        await suspension.quiesce_org_calls(
            ORG, since=datetime.now(UTC) - timedelta(minutes=2),
            valkey=valkey, settings=_settings(),
        )
        assert provider.revoked[0][:2] == (f"call_{CALL}", "author")
        assert provider.deleted == []
        assert (await registry.get(CALL)).state is SessionState.TERMINAL
    finally:
        await valkey.aclose()


@pytest.mark.asyncio
@pytest.mark.verifies("FR-LIV-017")
async def test_failed_revocation_blocks_room_deletion_and_decision(monkeypatch) -> None:
    provider = _Provider(room=f"call_{CALL}", fail_revocation=True)
    monkeypatch.setattr(suspension.api, "LiveKitAPI", lambda *args, **kwargs: provider)
    valkey = fakeredis.aioredis.FakeRedis(decode_responses=True)
    registry = CallRegistry(valkey)
    call = CallSession(
        call_id=CALL, mode="test", participant_kind="author", participant_id=PERSON,
        participant_identity="author", participant_display_name="Test",
        org_id=ORG, team_id=ORG, drill_id=DRILL,
        attempt_id=None, state=SessionState.ESTABLISHED,
    )
    try:
        await registry.put(call)
        with pytest.raises(suspension.CallQuiescenceUnverified):
            await suspension.quiesce_org_calls(
                ORG, since=datetime.now(UTC) - timedelta(minutes=2),
                valkey=valkey, settings=_settings(),
            )
        assert provider.deleted == []
        assert (await registry.get(CALL)).state is SessionState.ESTABLISHED
    finally:
        await valkey.aclose()


@pytest.mark.asyncio
@pytest.mark.verifies("FR-LIV-017")
async def test_known_room_is_deleted_and_registry_terminated(monkeypatch) -> None:
    provider = _Provider(room=f"call_{CALL}")
    monkeypatch.setattr(suspension.api, "LiveKitAPI", lambda *args, **kwargs: provider)
    valkey = fakeredis.aioredis.FakeRedis(decode_responses=True)
    registry = CallRegistry(valkey)
    call = CallSession(
        call_id=CALL, mode="test", participant_kind="author", participant_id=PERSON,
        participant_identity="author", participant_display_name="Test",
        org_id=ORG, team_id=ORG, drill_id=DRILL,
        attempt_id=None, state=SessionState.ESTABLISHED,
    )
    try:
        await registry.put(call)
        await suspension.quiesce_org_calls(
            ORG, since=datetime.now(UTC) - timedelta(minutes=2),
            valkey=valkey, settings=_settings(),
        )
        assert provider.deleted == [f"call_{CALL}"]
        assert len(provider.revoked) == 1
        assert provider.revoked[0][:2] == (f"call_{CALL}", "author")
        assert provider.revoked[0][2] >= int(datetime.now(UTC).timestamp())
        assert (await registry.get(CALL)).state is SessionState.TERMINAL
    finally:
        await valkey.aclose()
