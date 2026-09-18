"""Isolated restore gate for lifecycle, purge, erasure, and object state."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import make_url
from valkey.asyncio import Valkey

from bluelab.adapters.erasure_ledger import (
    ErasureLedger,
    ErasureMarker,
    create_erasure_ledger,
)
from bluelab.adapters.lifecycle_history import (
    LifecycleHistory,
    create_lifecycle_history,
)
from bluelab.adapters.object_store import RestoreObjectStore, create_object_store
from bluelab.lifecycle.purge import execute_run
from bluelab.lifecycle.restore import (
    RestoreUnverified,
    ensure_replay_pending,
    reconcile_completed_purge_objects,
    replay_organization,
)
from bluelab.modules.operations.reconciliation import reconcile_objects
from bluelab.modules.operations.subject_rights import execute_erasure
from bluelab.platform.config import get_settings
from bluelab.platform.db.engine import dispose_engine
from bluelab.platform.db.privileged import system_scope
from bluelab.platform.db.scope import ScopeContext
from bluelab.platform.db.session import scoped_transaction
from bluelab.platform.security.sessions import SessionStore
from bluelab.platform.telemetry.logging import get_logger

_log = get_logger(__name__)


async def _database_org_ids() -> set[UUID]:
    """Find every organization reference retained in the restored database."""
    async with scoped_transaction(ScopeContext.anonymous()) as db:
        return set((await db.execute(text(
            "select id from org union select org_id from org_lifecycle_operation "
            "union select org_id from org_deletion_restriction "
            "union select org_id from org_purge_run "
            "union select org_id from erasure_request "
            "union select org_id from export_request"
        ))).scalars())


async def _verify_lifecycle_restore(
    *, history: LifecycleHistory, markers: Sequence[ErasureMarker],
    ledger: ErasureLedger, object_store: RestoreObjectStore,
    sessions: SessionStore,
) -> tuple[set[UUID], set[UUID], dict[str, UUID]]:
    """Verify the catalog, replay ordered decisions, and prove purge ownership."""
    catalog = set(await history.catalog_orgs())
    database_orgs = await _database_org_ids()
    marker_orgs = {marker.org_id for marker in markers}
    staged_orgs = {
        UUID(ref.key.split("/", 2)[1])
        for ref in await object_store.list_prefix("orgs/")
    }
    if not (database_orgs | marker_orgs | staged_orgs) <= catalog:
        raise RestoreUnverified("restored identifier is absent from lifecycle catalog")
    purged: set[UUID] = set()
    export_owners: dict[str, UUID] = {}
    for org_id in sorted(catalog):
        await history.repair_interrupted_receipt(org_id)
        await history.reconstruct_head_from_receipts(org_id)
        events = await history.verified_events(org_id)
        await replay_organization(
            org_id, events, history=history, markers=markers,
            ledger=ledger, object_store=object_store, sessions=sessions,
        )
        async with scoped_transaction(system_scope(org_id=org_id)) as db:
            pending_run = (await db.execute(text(
                "select id,status,inventory_manifest_key from org_purge_run where org_id=:org "
                "and status<>'completed' order by created_at desc limit 1"
            ), {"org": org_id})).first()
        if pending_run is not None:
            if pending_run.status == "needs_attention":
                raise RestoreUnverified("purge run needs operator resolution")
            if pending_run.inventory_manifest_key is not None:
                await reconcile_completed_purge_objects(
                    org_id, pending_run.id, history=history,
                    object_store=object_store,
                )
            await execute_run(
                org_id=org_id, run_id=pending_run.id, history=history,
                object_store=object_store, sessions=sessions,
            )
            events = await history.verified_events(org_id)
        if events and events[-1].action == "complete":
            purged.add(org_id)
            run_id = UUID(events[-1].data["run_id"])
            key = f"org-purge/{org_id}/{run_id}/inventory.json"
            digest, manifest = await history.verified_purge_record(key)
            if (digest != events[-1].data["inventory_manifest_digest"]
                    or manifest.get("org_id") != str(org_id)
                    or manifest.get("run_id") != str(run_id)):
                raise RestoreUnverified("completed purge inventory is unverified")
            for object_key in manifest["objects"]:
                if object_key.startswith("exports/"):
                    existing = export_owners.setdefault(object_key, org_id)
                    if existing != org_id:
                        raise RestoreUnverified("export object has conflicting owners")
    async with scoped_transaction(ScopeContext.anonymous()) as db:
        pending = (await db.execute(text(
            "select org_id from org_lifecycle_operation where status='pending' limit 1"
        ))).scalar_one_or_none()
    if pending is not None:
        raise RestoreUnverified("unresolved lifecycle operation remains after replay")
    return catalog, purged, export_owners


async def _replay_remaining_markers(
    markers: Sequence[ErasureMarker], *, ledger: ErasureLedger,
    object_store: RestoreObjectStore, purged: set[UUID],
) -> None:
    for marker in markers:
        async with scoped_transaction(system_scope(org_id=marker.org_id)) as db:
            needs_replay = await ensure_replay_pending(db, marker)
        if needs_replay:
            if marker.org_id in purged:
                raise RestoreUnverified("completed purge left subject data to erase")
            async with scoped_transaction(system_scope(org_id=marker.org_id)) as db:
                await execute_erasure(
                    db, request_id=marker.request_id,
                    ledger=ledger, object_store=object_store,
                )


async def _record_purge_overlap(markers: Sequence[ErasureMarker], purged: set[UUID]) -> None:
    for marker in markers:
        if marker.org_id in purged:
            async with scoped_transaction(system_scope(org_id=marker.org_id)) as db:
                await db.execute(text(
                    "update erasure_request set status='executed', "
                    "evidence=jsonb_build_object('recovery','verified_org_purge_overlap') "
                    "where id=:request and status<>'executed'"
                ), {"request": marker.request_id})


async def _run() -> None:
    settings = get_settings()
    restore_url = make_url(settings.database_url.get_secret_value())
    migration_url = (make_url(settings.migration_database_url.get_secret_value())
                     if settings.migration_database_url else None)
    if (migration_url is None or restore_url.username != migration_url.username
            or restore_url.host != migration_url.host
            or restore_url.port != migration_url.port
            or restore_url.database != migration_url.database):
        raise RuntimeError("restore reconciliation requires the isolated migration role")
    if os.environ.get("RESTORE_ISOLATION_CONFIRMED") != "true":
        raise RuntimeError("restore reconciliation requires confirmed traffic and object isolation")
    history = create_lifecycle_history(settings)
    ledger = create_erasure_ledger(settings)
    object_store = create_object_store(settings)
    markers = await ledger.markers()
    valkey = Valkey.from_url(settings.valkey_url.get_secret_value(), decode_responses=True)
    sessions = SessionStore(
        valkey, idle_seconds=settings.session_idle_seconds,
        absolute_seconds=settings.session_absolute_seconds,
    )
    try:
        catalog, purged, export_owners = await _verify_lifecycle_restore(
            history=history, markers=markers, ledger=ledger,
            object_store=object_store, sessions=sessions,
        )
        await _replay_remaining_markers(
            markers, ledger=ledger, object_store=object_store, purged=purged,
        )
        async with scoped_transaction(ScopeContext.anonymous()) as db:
            evidence = await reconcile_objects(
                db, object_store=object_store, catalog_orgs=catalog,
                purged_orgs=purged, purge_export_owners=export_owners,
            )
        await _record_purge_overlap(markers, purged)
        _log.info("restore_reconciliation_completed", organizations=len(catalog),
                  purged_organizations=len(purged), **evidence)
    finally:
        await valkey.aclose()


async def _main() -> None:
    try:
        await _run()
    finally:
        await dispose_engine()


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
