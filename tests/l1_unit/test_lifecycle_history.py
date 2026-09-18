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

    async def list_keys(self, prefix: str) -> list[str]:
        return sorted(key for key in self.items if key.startswith(prefix))

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
    await history.register(ORG)
    first = await append(history, FIRST, 0)
    second = await append(history, SECOND, 1, "v2")
    assert first.sequence == 1
    assert second.previous_digest == first.digest
    assert (await history.verified_head(ORG)).sequence == 2
    assert await history.verified_events(ORG) == [first, second]
    assert await append(history, FIRST, 0) == first
    with pytest.raises(HistoryConflict):
        await append(history, FIRST, 0, "changed")
    with pytest.raises(HistoryConflict):
        await append(history, UUID("01800000-0000-7000-8000-000000000004"), 0)


@pytest.mark.asyncio
async def test_protected_head_exposes_missing_or_modified_events() -> None:
    store = MemoryStore()
    history = LifecycleHistory(store)
    await history.register(ORG)
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
    await history.register(ORG)
    await append(history, FIRST, 0)
    await append(history, SECOND, 1)
    del store.items[f"org-lifecycle/{ORG}/events/{FIRST}.json"]
    with pytest.raises(HistoryUnverified):
        await history.verified_head(ORG)


@pytest.mark.asyncio
async def test_backup_receipt_or_catalog_loss_blocks_history_release() -> None:
    live, backup = MemoryStore(), MemoryStore()
    history = LifecycleHistory(live, backup)
    await history.register(ORG)
    await append(history, FIRST, 0)
    assert (await history.catalog_orgs()) == [ORG]
    del backup.items[f"org-lifecycle/catalog/receipts/{ORG}/1.json"]
    with pytest.raises(HistoryUnverified):
        await history.verified_head(ORG)
    del backup.items[f"org-lifecycle/catalog/orgs/{ORG}.json"]
    with pytest.raises(HistoryUnverified):
        await history.catalog_orgs()


@pytest.mark.asyncio
async def test_purge_manifest_requires_matching_immutable_dual_copies() -> None:
    live, backup = MemoryStore(), MemoryStore()
    history = LifecycleHistory(live, backup)
    key = f"org-purge/{ORG}/{FIRST}/inventory.json"
    digest = await history.put_purge_record(key, {"ids": [str(SECOND)]})
    assert (await history.read_purge_record(key, digest)) == {"ids": [str(SECOND)]}
    with pytest.raises(HistoryConflict):
        await history.put_purge_record(key, {"ids": []})
    backup.items[key] = (b'{"ids":[]}', backup.items[key][1])
    with pytest.raises(HistoryUnverified):
        await history.read_purge_record(key, digest)


@pytest.mark.asyncio
async def test_repair_only_verified_interrupted_tail_receipt() -> None:
    live, backup = MemoryStore(), MemoryStore()
    history = LifecycleHistory(live, backup)
    await history.register(ORG)
    await append(history, FIRST, 0)
    receipt_key = f"org-lifecycle/catalog/receipts/{ORG}/1.json"
    del backup.items[receipt_key]
    with pytest.raises(HistoryUnverified):
        await history.verified_head(ORG)
    assert await history.repair_interrupted_receipt(ORG)
    assert (await history.verified_head(ORG)).sequence == 1
    assert not await history.repair_interrupted_receipt(ORG)

    del live.items[receipt_key]
    del backup.items[receipt_key]
    del backup.items[f"org-lifecycle/{ORG}/events/{FIRST}.json"]
    with pytest.raises(HistoryUnverified):
        await history.repair_interrupted_receipt(ORG)


@pytest.mark.asyncio
async def test_reconstructs_missing_head_only_from_complete_dual_receipts() -> None:
    live, backup = MemoryStore(), MemoryStore()
    history = LifecycleHistory(live, backup)
    await history.register(ORG)
    await append(history, FIRST, 0)
    del live.items[f"org-lifecycle/{ORG}/head.json"]
    assert (await history.reconstruct_head_from_receipts(ORG)).sequence == 1
    del live.items[f"org-lifecycle/{ORG}/head.json"]
    del backup.items[f"org-lifecycle/catalog/receipts/{ORG}/1.json"]
    with pytest.raises(HistoryUnverified):
        await history.reconstruct_head_from_receipts(ORG)
