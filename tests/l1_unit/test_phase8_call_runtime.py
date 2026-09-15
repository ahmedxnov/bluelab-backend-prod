"""Phase 8 call-plane coordination and concealment contracts."""

from __future__ import annotations

from uuid import uuid4

import fakeredis.aioredis
import pytest

from bluelab.calls.egress import EgressRegistry
from bluelab.calls.internal_router import CompletionBody
from bluelab.calls.lease import CallLease, CallLeaseStore, LeaseState
from bluelab.calls.placement import mint_participant_token
from bluelab.calls.registry import CallRegistry, CallSession
from bluelab.calls.webhook_router import _call_id
from bluelab_runtime_bundle import RuntimeBundle


@pytest.mark.asyncio
async def test_call_lease_is_participant_keyed_and_owner_checked() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = CallLeaseStore(client, ttl_seconds=90)
    participant_id = uuid4()
    first = CallLease.pending(
        participant_kind="rep",
        participant_id=participant_id,
        call_id=uuid4(),
        attempt_id=uuid4(),
    )
    second = CallLease.pending(
        participant_kind="rep",
        participant_id=participant_id,
        call_id=uuid4(),
        attempt_id=uuid4(),
    )

    assert await store.acquire(first)
    assert not await store.acquire(second)
    assert not await store.release(second)
    assert (await store.current("rep", participant_id)) == first
    assert await store.mark_established(first)
    assert (await store.current("rep", participant_id)).state is LeaseState.ESTABLISHED
    assert await store.release(first)
    assert await store.current("rep", participant_id) is None
    assert not await store.mark_established(first)

    await client.aclose()


@pytest.mark.asyncio
async def test_egress_start_and_reordered_outcome_are_idempotent() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    registry = EgressRegistry(client)
    call = CallSession(
        call_id=uuid4(),
        mode="attempt",
        participant_kind="rep",
        participant_id=uuid4(),
        participant_identity="acct_x",
        participant_display_name="Rep",
        org_id=uuid4(),
        team_id=uuid4(),
        drill_id=uuid4(),
        attempt_id=uuid4(),
        account_role="rep",
    )

    assert await registry.claim_start(call)
    assert not await registry.claim_start(call)
    await registry.started(call, "egress-1")
    assert await registry.matches(call, "egress-1")
    assert not await registry.matches(call, "egress-other")
    assert await registry.outcome(call) is None
    await registry.set_outcome(call, available=True)
    assert await registry.outcome(call) is True
    await client.aclose()


def test_runtime_bundle_rejects_concealed_and_unknown_fields() -> None:
    payload = {
        "contract_version": 1,
        "call_id": str(uuid4()),
        "mode": "test",
        "attempt_id": None,
        "drill_id": str(uuid4()),
        "org_id": str(uuid4()),
        "team_id": str(uuid4()),
        "participant": {
            "kind": "author",
            "identity": "acct_x",
            "display_name": "Author",
        },
        "scenario": {
            "buyer_name": "Mona",
            "buyer_role": "Buyer",
            "buyer_company": "Acme",
            "situation_context": "Renewal discussion",
            "participant_product_summary": "A participant-safe summary",
        },
        "persona": {
            "who_you_are": "A buyer",
            "your_world": "An Egyptian company",
            "where_you_are_right_now": "Reviewing renewal",
        },
        "language": "ar-EG",
        "call_type": "renewal",
        "lead_type": None,
        "voice_identity": "egyptian-female-1",
        "answer_key": {"must": "never cross the seam"},
    }

    with pytest.raises(ValueError):
        RuntimeBundle.model_validate(payload)


def test_livekit_token_has_minimal_single_room_grant() -> None:
    import jwt

    call = CallSession(
        call_id=uuid4(),
        mode="attempt",
        participant_kind="rep",
        participant_id=uuid4(),
        participant_identity="acct_123",
        participant_display_name="Rep",
        org_id=uuid4(),
        team_id=uuid4(),
        drill_id=uuid4(),
        attempt_id=uuid4(),
        account_role="rep",
    )
    token = mint_participant_token(
        api_key="key",
        api_secret="secret-secret-secret-secret-secret12",
        call=call,
    )
    claims = jwt.decode(
        token,
        "secret-secret-secret-secret-secret12",
        algorithms=["HS256"],
        options={"verify_aud": False},
    )
    assert claims["video"] == {
        "roomJoin": True,
        "room": f"call_{call.call_id}",
        "canPublish": True,
        "canSubscribe": True,
        "canPublishData": True,
        "canPublishSources": ["microphone"],
    }
    assert claims["sub"] == "acct_123"
    assert claims["roomConfig"]["agents"][0]["agentName"] == "bluelab-buyer"
    dispatch = __import__("json").loads(
        claims["roomConfig"]["agents"][0]["metadata"]
    )
    assert set(dispatch) == {
        "attempt_id",
        "call_id",
        "contract_version",
        "drill_id",
        "max_call_seconds",
        "mode",
        "org_id",
        "participant",
        "reconnect_grace_seconds",
        "team_id",
    }
    assert dispatch["participant"] == {
        "display_name": "Rep",
        "identity": "acct_123",
        "kind": "rep",
    }
    assert 59 <= claims["exp"] - claims["nbf"] <= 60


def test_completion_contract_rejects_unsorted_or_buyer_demeanor_transcript() -> None:
    call_id = uuid4()
    base = {
        "contract_version": 1,
        "call_id": call_id,
        "disposition": "completed",
        "duration_seconds": 12.3,
    }
    with pytest.raises(ValueError):
        CompletionBody.model_validate(
            {
                **base,
                "transcript": [
                    {
                        "sequence_index": 2,
                        "speaker": "buyer",
                        "text": "b",
                        "timestamp_seconds": 2,
                        "demeanor_label": None,
                    },
                    {
                        "sequence_index": 1,
                        "speaker": "participant",
                        "text": "a",
                        "timestamp_seconds": 1,
                        "demeanor_label": "calm",
                    },
                ],
            }
        )
    with pytest.raises(ValueError):
        CompletionBody.model_validate(
            {
                **base,
                "transcript": [
                    {
                        "sequence_index": 1,
                        "speaker": "buyer",
                        "text": "b",
                        "timestamp_seconds": 1,
                        "demeanor_label": "calm",
                    },
                ],
            }
        )


def test_webhook_room_name_parser_is_closed() -> None:
    call_id = uuid4()
    assert _call_id(f"call_{call_id}") == call_id
    assert _call_id(f"other_{call_id}") is None
    assert _call_id("call_not-an-id") is None


@pytest.mark.asyncio
async def test_registry_surfaces_due_calls_for_recovery() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    registry = CallRegistry(client)
    call = CallSession(
        call_id=uuid4(),
        mode="test",
        participant_kind="author",
        participant_id=uuid4(),
        participant_identity="acct_x",
        participant_display_name="Author",
        org_id=uuid4(),
        team_id=uuid4(),
        drill_id=uuid4(),
        attempt_id=None,
        account_role="manager",
    )
    await registry.put(call, recover_at_epoch=10)
    assert await registry.due(now_epoch=9) == []
    assert await registry.due(now_epoch=10) == [call]
    await client.aclose()
