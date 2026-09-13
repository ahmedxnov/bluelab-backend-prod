"""Bounded authentication backoff — increasing delay without account lockout."""

from __future__ import annotations

import asyncio

import fakeredis.aioredis
import pytest

from bluelab.platform.security.throttle import Throttle

pytestmark = [pytest.mark.l1_unit, pytest.mark.l7_security]


@pytest.mark.verifies("SEC-004")
async def test_failure_holds_grow_exponentially_and_success_can_clear_them():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    throttle = Throttle(client)
    try:
        assert (
            await throttle.record_failure(
                "identifier",
                "person@example.com",
                threshold=2,
                window_seconds=60,
                base_seconds=1,
                max_seconds=8,
            )
            is None
        )
        assert (
            await throttle.record_failure(
                "identifier",
                "person@example.com",
                threshold=2,
                window_seconds=60,
                base_seconds=1,
                max_seconds=8,
            )
            == 1
        )
        assert await throttle.backoff_remaining("identifier", "person@example.com") == 1

        await asyncio.sleep(1.05)
        assert (
            await throttle.record_failure(
                "identifier",
                "person@example.com",
                threshold=2,
                window_seconds=60,
                base_seconds=1,
                max_seconds=8,
            )
            == 2
        )

        await throttle.clear_failures("identifier", "person@example.com")
        assert await throttle.backoff_remaining("identifier", "person@example.com") is None
        assert (
            await throttle.record_failure(
                "identifier",
                "person@example.com",
                threshold=2,
                window_seconds=60,
                base_seconds=1,
                max_seconds=8,
            )
            is None
        )
    finally:
        await client.aclose()
