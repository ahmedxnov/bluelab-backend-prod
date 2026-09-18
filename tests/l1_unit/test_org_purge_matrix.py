"""The purge inventory must cover every organization-owned schema table."""

from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from bluelab.adapters.object_store import ObjectRef
from bluelab.lifecycle import purge
from bluelab.lifecycle.purge_matrix import (
    RETAINED_TABLES,
    SHARED_TABLES,
    STEP_ORDER,
    STEP_TABLES,
    batch_keys,
    table_primary_keys,
)
from bluelab.platform.db.base import Base


def test_matrix_covers_every_owned_table_once() -> None:
    tables = [table for step in STEP_ORDER for table in STEP_TABLES.get(step, ())]
    assert len(tables) == len(set(tables))
    table_primary_keys("org")
    owned = {
        table.name for table in Base.metadata.tables.values()
        if "org_id" in table.c or table.name == "org"
    }
    assert owned - RETAINED_TABLES == set(tables) - {"password_reset_token"}
    assert not (set(tables) & SHARED_TABLES)
    assert table_primary_keys("assignment_recipient") == ("assignment_id", "rep_account_id")


def test_batches_never_exceed_one_thousand_rows() -> None:
    inventory = {
        table: [] for step in STEP_ORDER for table in STEP_TABLES.get(step, ())
    }
    inventory["account"] = [{"id": str(index)} for index in range(1001)]
    batches = [batch for batch in batch_keys(inventory) if batch["target_table"] == "account"]
    assert [len(batch["keys"]) for batch in batches] == [1000, 1]
    assert [batch["batch_key"] for batch in batches] == [
        "account:00000000", "account:00000001",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("target_remains", [False, True])
async def test_purge_verifies_inventoried_exact_object_without_listing(
    monkeypatch, target_remains,
) -> None:
    org_id, attempt_id = uuid4(), uuid4()
    ref = ObjectRef.recording(org_id=org_id, attempt_id=attempt_id)
    checked = []

    @asynccontextmanager
    async def transaction(_scope):
        yield object()

    async def rows(_db, _org):
        return {"org": [{"id": str(org_id)}]}

    async def jobs(_db, _org):
        return []

    class Objects:
        async def list_prefix(self, prefix):
            raise AssertionError("purge must not enumerate application objects")

        async def exists(self, item):
            checked.append(item.key)
            return target_remains

    monkeypatch.setattr(purge, "scoped_transaction", transaction)
    monkeypatch.setattr(purge, "inventory_rows", rows)
    monkeypatch.setattr(purge, "inventory_jobs", jobs)
    initial_rows = {
        table: [] for step in STEP_ORDER for table in STEP_TABLES.get(step, ())
    }
    if target_remains:
        with pytest.raises(purge.PurgeBlocked, match="inventoried object remains"):
            await purge._verify_cleanup(
                org_id=org_id, initial={"rows": initial_rows, "objects": [ref.key]},
                object_store=Objects(),
            )
    else:
        result = await purge._verify_cleanup(
            org_id=org_id, initial={"rows": initial_rows, "objects": [ref.key]},
            object_store=Objects(),
        )
        assert result["objects"] == 1
    assert checked == [ref.key]
