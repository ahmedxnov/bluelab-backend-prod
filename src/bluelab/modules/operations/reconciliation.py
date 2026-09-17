"""Mandatory post-restore erasure replay and two-way object reconciliation."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.adapters.object_store import DeletionReason, RestoreObjectStore
from bluelab.platform.telemetry.logging import get_logger

_log = get_logger(__name__)


async def reconcile_objects(
    session: AsyncSession, *, object_store: RestoreObjectStore
) -> dict[str, int]:
    """Make database references and the closed object prefixes agree."""
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
    for key in sorted(expected - actual.keys()):
        category = await session.scalar(
            text("select app_mark_missing_object(:key)"), {"key": key}
        )
        _log.error("restore_object_missing", object_category=category)
    for key in sorted(actual.keys() - expected):
        await object_store.delete(actual[key], reason=DeletionReason.SWEEP)
    evidence = {
        "missing_references_repaired": len(expected - actual.keys()),
        "orphan_objects_deleted": len(actual.keys() - expected),
    }
    _log.info("restore_object_reconciliation_completed", **evidence)
    return evidence
