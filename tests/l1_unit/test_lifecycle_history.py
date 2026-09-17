"""Independent lifecycle history detects missing tails, gaps, and reuse."""

from uuid import UUID

import pytest

from bluelab.adapters.lifecycle_history import (
    HistoryConflict,
    HistoryUnverified,
    LifecycleHistory,
)


class MemoryStore:
    def __init__(self) -> None:
        self.items: dict[str, tuple[bytes, str]] = {}
        self.version = 0

    async def read(self, key: str) -> tuple[bytes, str] | None:
        return self.items.get(key)

    async def put(self, key: str, body: bytes, *, expected_etag: str | None) -> None:
        current = self.items.get(key)
        if (current is None and expected_etag is not None) or (
            current is not None and current[1] != expected_etag
        ):
            raise HistoryConflict("conditional write rejected")
        self.version += 1
        self.items[key] = (body, str(self.version))


ORG = UUID("01800000-0000-7000-8000-000000000001")
FIRST = UUID("01800000-0000-7000-8000-000000000002")
SECOND = UUID("01800000-0000-7000-8000-000000000003")


async def append(history: LifecycleHistory, operation_id: UUID, sequence: int, value: str = "v1"):
    return await history.append(
        org_id=ORG, operation_id=operation_id, expected_sequence=sequence,
        action="confirm_term", accepted_at="2026-01-01T00:00:00+00:00",
        actor_id=None, reason="contract", data={"contract_reference": value},
    )


@pytest.mark.asyncio
async def test_append_verifies_order_and_replays_same_operation() -> None:
    history = LifecycleHistory(MemoryStore())
    first = await append(history, FIRST, 0)
    second = await append(history, SECOND, 1, "v2")
    assert first.sequence == 1
    assert second.previous_digest == first.digest
    assert (await history.verified_head(ORG)).sequence == 2
    assert await append(history, FIRST, 0) == first
    with pytest.raises(HistoryConflict):
        await append(history, FIRST, 0, "changed")
    with pytest.raises(HistoryConflict):
        await append(history, UUID("01800000-0000-7000-8000-000000000004"), 0)


@pytest.mark.asyncio
async def test_protected_head_exposes_missing_or_modified_events() -> None:
    store = MemoryStore()
    history = LifecycleHistory(store)
    await append(history, FIRST, 0)
    key = f"org-lifecycle/{ORG}/events/{FIRST}.json"
    body, etag = store.items[key]
    store.items[key] = (body.replace(b"contract", b"altered!"), etag)
    with pytest.raises(HistoryUnverified):
        await history.verified_head(ORG)


@pytest.mark.asyncio
async def test_missing_predecessor_is_not_a_valid_shorter_history() -> None:
    store = MemoryStore()
    history = LifecycleHistory(store)
    await append(history, FIRST, 0)
    await append(history, SECOND, 1)
    del store.items[f"org-lifecycle/{ORG}/events/{FIRST}.json"]
    with pytest.raises(HistoryUnverified):
        await history.verified_head(ORG)
