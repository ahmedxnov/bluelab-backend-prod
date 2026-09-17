"""Procrastinate-owned retention and stale-recording schedules."""

from __future__ import annotations

from uuid import UUID

import procrastinate

from bluelab.adapters.object_store import ObjectStore, create_object_store
from bluelab.modules.operations.retention import (
    RETENTION_CLASSES,
    RetentionClass,
    sweep,
    sweep_stale_recordings,
)
from bluelab.platform.config import Settings
from bluelab.platform.db.privileged import system_scope
from bluelab.platform.db.session import scoped_transaction

_MAINTENANCE_ORG_ID = UUID(int=0)


async def run_retention_class(
    retention_class: RetentionClass, object_store: ObjectStore
) -> None:
    async with scoped_transaction(system_scope(org_id=_MAINTENANCE_ORG_ID)) as db:
        await sweep(db, retention_class=retention_class, object_store=object_store)


def register_maintenance(app: procrastinate.App, settings: Settings) -> None:
    """Each class has an independent daily job and failure outcome."""
    object_store = create_object_store(settings)
    for retention_class in RETENTION_CLASSES:
        async def run(timestamp: int, selected: RetentionClass = retention_class) -> None:
            await run_retention_class(selected, object_store)

        run.__name__ = f"sweep_{retention_class.lower().replace('-', '_')}"
        task = app.task(
            name=f"retention_{retention_class.lower().replace('-', '_')}",
            queue="maintenance",
        )(run)
        app.periodic(cron="0 2 * * *")(task)

    @app.periodic(cron="10 * * * *")
    @app.task(name="sweep_stale_pending_recordings", queue="maintenance")
    async def stale_recordings(timestamp: int) -> None:
        async with scoped_transaction(system_scope(org_id=_MAINTENANCE_ORG_ID)) as db:
            await sweep_stale_recordings(db)
