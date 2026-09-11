"""RFC 6238 operations-factor and replay controls."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import fakeredis.aioredis
import pytest

from bluelab.platform.security import sessions
from bluelab.platform.security.sessions import OpsSessionStore
from bluelab.platform.security.totp import (
    STEP_SECONDS,
    TotpReplayStore,
    code_for_step,
    matching_step,
)

pytestmark = [pytest.mark.l1_unit, pytest.mark.l7_security]

RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # pragma: allowlist secret -- RFC 6238 test vector
OPERATOR = UUID("01936d54-7ad5-7000-8000-000000000001")


@pytest.mark.verifies("ADR-0028")
def test_totp_uses_rfc_vector_and_only_one_adjacent_step():
    at = datetime.fromtimestamp(59, tz=UTC)
    current = int(at.timestamp()) // STEP_SECONDS

    # RFC 6238's SHA-1 vector at 59 seconds is 94287082; six-digit mode keeps
    # its final six digits.
    assert code_for_step(RFC_SECRET, current) == "287082"
    assert matching_step(RFC_SECRET, "287082", at=at) == current
    assert matching_step(RFC_SECRET, code_for_step(RFC_SECRET, current - 1), at=at) == current - 1
    assert matching_step(RFC_SECRET, code_for_step(RFC_SECRET, current + 1), at=at) == current + 1
    assert matching_step(RFC_SECRET, code_for_step(RFC_SECRET, current + 2), at=at) is None
    assert matching_step(RFC_SECRET, "abcdef", at=at) is None


@pytest.mark.asyncio
@pytest.mark.verifies("ADR-0028")
async def test_totp_step_can_be_claimed_only_once_under_race():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        replay = TotpReplayStore(client)
        claims = await asyncio.gather(
            replay.claim(ops_account_id=OPERATOR, step=42),
            replay.claim(ops_account_id=OPERATOR, step=42),
        )
        assert sorted(claims) == [False, True]
        assert await replay.claim(ops_account_id=OPERATOR, step=43) is True
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.verifies("SEC-001")
async def test_ops_session_enforces_idle_and_absolute_expiry(monkeypatch):
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    opened = datetime(2026, 1, 1, tzinfo=UTC)
    store = OpsSessionStore(client, idle_seconds=10, absolute_seconds=20)
    try:
        idle_raw = await store.create(ops_account_id=OPERATOR, opened_at=opened)
        monkeypatch.setattr(sessions, "now", lambda: opened + timedelta(seconds=11))
        assert await store.resolve(idle_raw) is None

        absolute_raw = await store.create(ops_account_id=OPERATOR, opened_at=opened)
        monkeypatch.setattr(sessions, "now", lambda: opened + timedelta(seconds=5))
        assert await store.resolve(absolute_raw) is not None
        monkeypatch.setattr(sessions, "now", lambda: opened + timedelta(seconds=21))
        assert await store.resolve(absolute_raw) is None
    finally:
        await client.aclose()
