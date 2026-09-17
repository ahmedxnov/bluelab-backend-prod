"""Blocking restore gate: replay erasures, then reconcile objects."""

from __future__ import annotations

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.adapters.erasure_ledger import ErasureMarker, create_erasure_ledger
from bluelab.adapters.object_store import create_object_store
from bluelab.modules.operations.reconciliation import reconcile_objects
from bluelab.modules.operations.subject_rights import execute_erasure
from bluelab.platform.config import get_settings
from bluelab.platform.db.engine import dispose_engine
from bluelab.platform.db.privileged import system_scope
from bluelab.platform.db.scope import ScopeContext
from bluelab.platform.db.session import scoped_transaction


async def ensure_replay_pending(db: AsyncSession, marker: ErasureMarker) -> bool:
    """Reopen an executed request whenever its restored rows fail zero-remain."""
    await db.execute(
        text(
            "select app_restore_erasure_marker(:request,:org,:kind,:subject,"
            ":requested,:actor)"
        ),
        {
            "request": marker.request_id,
            "org": marker.org_id,
            "kind": marker.subject_kind,
            "subject": marker.subject_id,
            "requested": marker.requested_at,
            "actor": marker.executed_by,
        },
    )
    remaining = dict(
        (
            await db.execute(
                text("select app_verify_erasure(:request)"),
                {"request": marker.request_id},
            )
        ).scalar_one()
    )
    needs_replay = any(int(count) for count in remaining.values())
    if needs_replay:
        await db.execute(
            text(
                "update erasure_request set status='pending' "
                "where id=:request and status='executed'"
            ),
            {"request": marker.request_id},
        )
    return needs_replay


async def _run() -> None:
    settings = get_settings()
    ledger = create_erasure_ledger(settings)
    object_store = create_object_store(settings)
    markers = await ledger.markers()
    for marker in markers:
        async with scoped_transaction(ScopeContext.anonymous()) as db:
            await ensure_replay_pending(db, marker)
        async with scoped_transaction(system_scope(org_id=marker.org_id)) as db:
            await execute_erasure(
                db,
                request_id=marker.request_id,
                ledger=ledger,
                object_store=object_store,
            )
    async with scoped_transaction(ScopeContext.anonymous()) as db:
        await reconcile_objects(db, object_store=object_store)


async def _main() -> None:
    try:
        await _run()
    finally:
        await dispose_engine()


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
