"""Mandatory post-restore erasure replay and two-way object reconciliation."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.adapters.object_store import DeletionReason, ObjectRef, RestoreObjectStore
from bluelab.platform.telemetry.logging import get_logger

_log = get_logger(__name__)


async def reconcile_objects(
    session: AsyncSession, *, object_store: RestoreObjectStore,
    catalog_orgs: set[UUID], purged_orgs: set[UUID],
    purge_export_owners: dict[str, UUID],
) -> dict[str, int]:
    """Reconcile closed prefixes only after every object has verified ownership."""
    expected = {
        str(key)
        for key in (
            await session.execute(text("select app_object_inventory()"))
        ).scalar_one()
    }
    actual_refs = [
        *(await object_store.list_prefix("orgs/")),
        *(await object_store.list_prefix("exports/")),
    ]
    actual = {ref.key: ref for ref in actual_refs}
    export_owners: dict[str, UUID] = {
        f"exports/{request_id}.zip": UUID(str(org_id))
        for request_id, org_id in (await session.execute(text(
            "select id,org_id from export_request"
        ))).all()
    }
    for key, purge_owner in purge_export_owners.items():
        if key in export_owners and export_owners[key] != purge_owner:
            raise RuntimeError("restored export has conflicting ownership evidence")
        export_owners[key] = purge_owner
    for key in actual:
        ref = ObjectRef(key)
        if ref.key.startswith("orgs/"):
            owner: UUID | None = UUID(ref.key.split("/", 2)[1])
        else:
            owner = export_owners.get(ref.key)
        if owner is None or owner not in catalog_orgs:
            raise RuntimeError("restored object has unverifiable ownership")
        if owner in purged_orgs and key in expected:
            raise RuntimeError("purged organization retains a database object reference")
    for key in sorted(expected - actual.keys()):
        category = await session.scalar(
            text("select app_mark_missing_object(:key)"), {"key": key}
        )
        _log.error("restore_object_missing", object_category=category)
    for key in sorted(actual.keys() - expected):
        await object_store.delete(actual[key], reason=DeletionReason.SWEEP)
        if await object_store.exists(actual[key]):
            raise RuntimeError("restored object deletion was not verified")
    evidence = {
        "missing_references_repaired": len(expected - actual.keys()),
        "orphan_objects_deleted": len(actual.keys() - expected),
    }
    _log.info("restore_object_reconciliation_completed", **evidence)
    return evidence
