"""Deterministic create-after-revoke regressions for both session namespaces."""

from __future__ import annotations

from uuid import UUID

import fakeredis.aioredis
import pytest

from bluelab.platform.security.sessions import (
    OpsSessionStore,
    SessionRevokedDuringCreation,
    SessionStore,
)
from bluelab.platform.security.tokens import hash_token

pytestmark = [pytest.mark.l1_unit, pytest.mark.l7_security]

ACCOUNT_ID = UUID("01936d54-7ad5-7000-8000-000000000001")
ORG_ID = UUID("01936d54-7ad5-7000-8000-000000000002")
TEAM_ID = UUID("01936d54-7ad5-7000-8000-000000000003")
OPS_ACCOUNT_ID = UUID("01936d54-7ad5-7000-8000-000000000004")


@pytest.mark.verifies("SEC-001")
async def test_customer_session_snapshotted_before_revoke_cannot_be_created_afterward():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = SessionStore(client, idle_seconds=60, absolute_seconds=300)
    epoch = await store.revocation_epoch(ACCOUNT_ID)

    assert await store.revoke_all(ACCOUNT_ID) == 0
    with pytest.raises(SessionRevokedDuringCreation):
        await store.create(
            account_id=ACCOUNT_ID,
            org_id=ORG_ID,
            team_id=TEAM_ID,
            role="rep",
            revocation_epoch=epoch,
        )

    await client.aclose()


@pytest.mark.verifies("SEC-001")
async def test_ops_session_snapshotted_before_revoke_cannot_be_created_afterward():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = OpsSessionStore(client, idle_seconds=60, absolute_seconds=300)
    epoch = await store.revocation_epoch(OPS_ACCOUNT_ID)

    assert await store.revoke_all(OPS_ACCOUNT_ID) == 0
    with pytest.raises(SessionRevokedDuringCreation):
        await store.create(
            ops_account_id=OPS_ACCOUNT_ID,
            revocation_epoch=epoch,
        )

    await client.aclose()


@pytest.mark.verifies("SEC-001")
async def test_customer_sign_out_rejects_a_stale_resolver_write():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = SessionStore(client, idle_seconds=60, absolute_seconds=300)
    raw = await store.create(
        account_id=ACCOUNT_ID,
        org_id=ORG_ID,
        team_id=TEAM_ID,
        role="rep",
    )
    key = "session:" + hash_token(raw)
    stale_payload = await client.get(key)

    await store.revoke(raw)
    await client.set(key, stale_payload, ex=60)

    assert await store.resolve(raw) is None
    assert await client.get(key) is None
    await client.aclose()


@pytest.mark.verifies("SEC-001")
async def test_ops_sign_out_rejects_a_stale_resolver_write():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = OpsSessionStore(client, idle_seconds=60, absolute_seconds=300)
    raw = await store.create(ops_account_id=OPS_ACCOUNT_ID)
    key = "ops:session:" + hash_token(raw)
    stale_payload = await client.get(key)

    await store.revoke(raw)
    await client.set(key, stale_payload, ex=60)

    assert await store.resolve(raw) is None
    assert await client.get(key) is None
    await client.aclose()
