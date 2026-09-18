"""Restored objects remain isolated until their owner is proven."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from bluelab.adapters.erasure_ledger import ErasureMarker
from bluelab.adapters.object_store import ObjectRef
from bluelab.lifecycle.restore import RestoreUnverified, ensure_replay_pending
from bluelab.modules.operations.reconciliation import reconcile_objects


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value

    def all(self):
        return self.value

    def first(self):
        return self.value


class _Session:
    async def execute(self, statement, values=None):
        if "app_object_inventory" in str(statement):
            return _Result([])
        if "from export_request" in str(statement):
            return _Result([])
        raise AssertionError("unexpected restore query")


class _Objects:
    def __init__(self, key: str) -> None:
        self.keys = {key}

    async def list_prefix(self, prefix: str):
        return [ObjectRef(key) for key in self.keys if key.startswith(prefix)]

    async def delete(self, ref, *, reason):
        self.keys.remove(ref.key)

    async def exists(self, ref):
        return ref.key in self.keys


@pytest.mark.asyncio
async def test_unknown_export_is_quarantined_by_restore_barrier() -> None:
    org = uuid4()
    key = f"exports/{uuid4()}.zip"
    objects = _Objects(key)
    with pytest.raises(RuntimeError, match="unverifiable ownership"):
        await reconcile_objects(
            _Session(), object_store=objects, catalog_orgs={org},
            purged_orgs=set(), purge_export_owners={},
        )
    assert objects.keys == {key}


@pytest.mark.asyncio
async def test_purged_org_object_is_deleted_and_verified() -> None:
    org = uuid4()
    key = f"orgs/{org}/recordings/{uuid4()}.ogg"
    objects = _Objects(key)
    evidence = await reconcile_objects(
        _Session(), object_store=objects, catalog_orgs={org},
        purged_orgs={org}, purge_export_owners={},
    )
    assert evidence["orphan_objects_deleted"] == 1
    assert not objects.keys


@pytest.mark.asyncio
async def test_restored_erasure_identity_cannot_override_armed_marker() -> None:
    marker = ErasureMarker(
        request_id=uuid4(), org_id=uuid4(), subject_kind="account",
        subject_id=uuid4(), requested_at=datetime.now(UTC),
        armed_at=datetime.now(UTC), executed_by=uuid4(),
    )

    class ConflictingSession:
        async def execute(self, statement, values=None):
            return _Result(SimpleNamespace(
                org_id=uuid4(), subject_kind="account", subject_id=marker.subject_id,
            ))

    with pytest.raises(RestoreUnverified, match="conflicts with its marker"):
        await ensure_replay_pending(ConflictingSession(), marker)
