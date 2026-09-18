"""Procrastinate-owned organization-purge discovery and recovery."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import UUID

import procrastinate
from sqlalchemy import text
from valkey.asyncio import Valkey

from bluelab.adapters.lifecycle_history import LifecycleHistory
from bluelab.adapters.object_store import RestoreObjectStore, create_object_store
from bluelab.lifecycle.purge import claim_one, execute_run, record_failure
from bluelab.modules.operations.models import OrgPurgeRun
from bluelab.platform.config import Settings
from bluelab.platform.db.privileged import system_scope
from bluelab.platform.db.session import scoped_transaction
from bluelab.platform.security.sessions import SessionStore
from bluelab.platform.telemetry.logging import get_logger

_log = get_logger(__name__)
_SCAN_SCOPE = UUID(int=0)
PurgeQuiescence = Callable[[UUID, datetime, Valkey, Settings], Awaitable[None]]


async def _queue(org_id: UUID, offboarding_id: UUID) -> bool:
    """Persist an episode-keyed job without depending on a sibling worker."""
    args = {"org_id": str(org_id), "offboarding_id": str(offboarding_id)}
    async with scoped_transaction(system_scope(org_id=org_id)) as db:
        await db.execute(text(
            "select pg_advisory_xact_lock(hashtextextended(:identity,0))"
        ), {"identity": f"org-purge-job:{org_id}:{offboarding_id}"})
        present = await db.scalar(text(
            "select id from procrastinate_jobs where task_name='execute_org_purge' "
            "and args=cast(:args as jsonb) and status in ('todo','doing') limit 1"
        ), {"args": json.dumps(args, separators=(",", ":"))})
        if present is not None:
            return False
        await db.execute(text(
            "insert into procrastinate_jobs(queue_name,task_name,args,status) "
            "values('maintenance','execute_org_purge',cast(:args as jsonb),'todo')"
        ), {"args": json.dumps(args, separators=(",", ":"))})
        return True


async def discover() -> dict[str, int]:
    """Discover eligible episodes hourly from the stored deadline."""
    async with scoped_transaction(system_scope(org_id=_SCAN_SCOPE)) as db:
        rows = (await db.execute(text(
            "select org_id,offboarding_id from app_due_org_purges(:at,:limit)"
        ), {"at": datetime.now(UTC), "limit": 1000})).all()
    queued = sum([await _queue(row.org_id, row.offboarding_id) for row in rows])
    _log.info("org_purge_discovery", discovered=len(rows), queued=queued)
    return {"discovered": len(rows), "queued": queued}


async def recover() -> dict[str, int]:
    """Requeue incomplete runs independently of the live organization row."""
    async with scoped_transaction(system_scope(org_id=_SCAN_SCOPE)) as db:
        rows = (await db.execute(text(
            "select run_id,org_id,offboarding_id,status,retry_at "
            "from app_incomplete_org_purges(:limit)"
        ), {"limit": 1000})).all()
    instant = datetime.now(UTC)
    eligible = [row for row in rows if row.status in {"pending", "running"}
                or (row.status == "retry_pending"
                    and (row.retry_at is None or row.retry_at <= instant))
                or row.status == "paused_restriction"]
    queued = sum([await _queue(row.org_id, row.offboarding_id) for row in eligible])
    attention = sum(row.status == "needs_attention" for row in rows)
    _log.info("org_purge_recovery_scan", incomplete=len(rows),
              queued=queued, needs_attention=attention)
    return {"incomplete": len(rows), "queued": queued, "needs_attention": attention}


async def execute_episode(
    org_id: UUID, offboarding_id: UUID, *, settings: Settings,
    history: LifecycleHistory, quiesce_calls: PurgeQuiescence,
) -> None:
    """Claim or resume one episode and leave uncertain outcomes visible."""
    valkey = Valkey.from_url(settings.valkey_url.get_secret_value(), decode_responses=True)
    object_store: RestoreObjectStore = create_object_store(settings)
    sessions = SessionStore(
        valkey, idle_seconds=settings.session_idle_seconds,
        absolute_seconds=settings.session_absolute_seconds,
    )
    try:
        async with scoped_transaction(system_scope(org_id=org_id)) as db:
            run = await db.scalar(text(
                "select id from org_purge_run where org_id=:org and offboarding_id=:episode"
            ), {"org": org_id, "episode": offboarding_id})
        if run is None:
            async def quiesce(target: UUID, since: datetime) -> None:
                await quiesce_calls(target, since, valkey, settings)
            claim = await claim_one(
                org_id, offboarding_id, history=history, quiesce=quiesce,
            )
            run_id = claim.id
        else:
            run_id = UUID(str(run))
        try:
            outcome = await execute_run(
                org_id=org_id, run_id=run_id, history=history,
                object_store=object_store, sessions=sessions,
            )
        except Exception as exc:
            async with scoped_transaction(system_scope(org_id=org_id)) as db:
                state = await db.get(OrgPurgeRun, run_id)
                already_recorded = state is not None and state.status in {
                    "retry_pending", "paused_restriction", "needs_attention",
                }
            if not already_recorded:
                await record_failure(
                    org_id=org_id, run_id=run_id, step_id=None, error=exc,
                )
            _log.error("org_purge_execution_failed", failure_class=type(exc).__name__)
            raise
        _log.info("org_purge_completed", categories=len(outcome.verification_summary))
    finally:
        await valkey.aclose()


def register_org_purge(
    app: procrastinate.App, settings: Settings, history: LifecycleHistory,
    quiesce_calls: PurgeQuiescence,
) -> None:
    """Install the hourly discovery, 15-minute recovery, and episode task."""
    if not settings.org_purge_enabled:
        return

    @app.task(name="execute_org_purge", queue="maintenance")
    async def execute_org_purge(org_id: str, offboarding_id: str) -> None:
        await execute_episode(
            UUID(org_id), UUID(offboarding_id), settings=settings, history=history,
            quiesce_calls=quiesce_calls,
        )

    @app.periodic(cron="5 * * * *")
    @app.task(name="discover_org_purges", queue="maintenance")
    async def discover_org_purges(timestamp: int) -> None:
        await discover()

    @app.periodic(cron="*/15 * * * *")
    @app.task(name="recover_org_purges", queue="maintenance")
    async def recover_org_purges(timestamp: int) -> None:
        await recover()
