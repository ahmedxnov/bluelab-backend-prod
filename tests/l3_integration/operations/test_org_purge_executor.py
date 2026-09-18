"""Minimal real-PostgreSQL purge: durable claim through org-row finalization."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import UUID

import fakeredis.aioredis
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from bluelab.adapters.lifecycle_history import HistoryConflict, LifecycleHistory
from bluelab.entrypoints.restore_reconcile import _verify_lifecycle_restore
from bluelab.lifecycle.purge import claim_one, execute_run
from bluelab.lifecycle.restore import (
    reconcile_completed_purge_objects,
    replay_organization,
)
from bluelab.platform.ids import new_id
from bluelab.platform.security.sessions import SessionStore


class _MemoryHistoryStore:
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


class _Objects:
    def __init__(self, key: str) -> None:
        self.keys = {key}
        self.fail_once = True

    async def list_prefix(self, prefix: str):
        from bluelab.adapters.object_store import ObjectRef

        return [ObjectRef(key) for key in sorted(self.keys) if key.startswith(prefix)]

    async def exists(self, ref):
        return ref.key in self.keys

    async def delete(self, ref, *, reason):
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("provider deletion unavailable")
        self.keys.discard(ref.key)


@pytest.mark.asyncio
@pytest.mark.verifies("SEC-043")
async def test_org_purge_claim_revokes_sessions_and_deletes_accounts() -> None:
    migration_url = os.environ.get("TEST_MIGRATION_URL")
    maintenance_url = os.environ.get("DATABASE_URL", "")
    if not migration_url or "bluelab_maintenance" not in maintenance_url:
        pytest.skip("isolated migration and maintenance database roles required")
    engine = create_async_engine(migration_url)
    org_id, episode_id, manager_id, rep_id, reset_id, export_id = (
        new_id() for _ in range(6)
    )
    instant = datetime.now(UTC)
    history = LifecycleHistory(_MemoryHistoryStore(), _MemoryHistoryStore())
    valkey = fakeredis.aioredis.FakeRedis(decode_responses=True)
    export_key = f"exports/{export_id}.zip"
    objects = _Objects(export_key)
    async def quiesce(org: UUID, since: datetime) -> None:
        assert org == org_id
        assert since < instant
    try:
        await history.register(org_id)
        async with engine.begin() as db:
            await db.execute(text(
                "insert into org(id,name,registered_domain,timezone,lifecycle_status,"
                "lifecycle_sequence,offboarding_id,offboarding_started_at,purge_eligible_at,"
                "retention_policy_reference) values "
                "(:id,'Purge fixture',:domain,'Africa/Cairo','offboarding',0,:episode,"
                ":started,:eligible,'org-default-90d:v1')"
            ), {
                "id": org_id, "domain": f"purge-{org_id}.example.test",
                "episode": episode_id, "started": instant - timedelta(days=91),
                "eligible": instant - timedelta(days=1),
            })
            await db.execute(text(
                "insert into account(id,org_id,team_id,email,display_name,role,"
                "password_hash,status) values "
                "(:manager,:org,:manager,:manager_email,'Manager','manager','x','active'),"
                "(:rep,:org,:manager,:rep_email,'Rep','rep','x','active')"
            ), {
                "manager": manager_id, "rep": rep_id, "org": org_id,
                "manager_email": f"manager-{manager_id}@example.test",
                "rep_email": f"rep-{rep_id}@example.test",
            })
            await db.execute(text(
                "insert into password_reset_token(id,account_id,token_hash,expires_at) "
                "values(:id,:account,:hash,:expires)"
            ), {
                "id": reset_id, "account": rep_id,
                "hash": str(reset_id), "expires": instant + timedelta(days=1),
            })
            await db.execute(text(
                "insert into export_request(id,org_id,subject_kind,subject_id,status,"
                "request_policy_reference,bundle_object_key) "
                "values(:id,:org,'account',:subject,'delivered','subject-rights:v1',:key)"
            ), {"id": export_id, "org": org_id, "subject": rep_id, "key": export_key})
        sessions = SessionStore(valkey, idle_seconds=3600, absolute_seconds=86400)
        raw_session = await sessions.create(
            account_id=rep_id, org_id=org_id, team_id=manager_id, role="rep",
        )
        assert await sessions.resolve(raw_session) is not None
        run = await claim_one(
            org_id, episode_id, history=history, quiesce=quiesce, at=instant,
        )
        assert run.status == "pending"
        with pytest.raises(RuntimeError, match="provider deletion unavailable"):
            await execute_run(
                org_id=org_id, run_id=run.id, history=history,
                object_store=objects,  # type: ignore[arg-type]
                sessions=sessions,
            )
        async with engine.begin() as db:
            assert await db.scalar(text(
                "select status from org_purge_run where id=:id"
            ), {"id": run.id}) == "retry_pending"
            await db.execute(text(
                "update org_purge_run set retry_at=:at where id=:id"
            ), {"at": instant - timedelta(seconds=1), "id": run.id})
        result = await execute_run(
            org_id=org_id, run_id=run.id, history=history,
            object_store=objects,  # type: ignore[arg-type]
            sessions=sessions,
        )
        assert result.status == "completed"
        assert await sessions.resolve(raw_session) is None
        assert not objects.keys
        assert (await history.verified_events(org_id))[-1].action == "complete"
        async with engine.connect() as db:
            assert await db.scalar(text("select count(*) from org where id=:id"), {"id": org_id}) == 0
            assert await db.scalar(text("select count(*) from account where org_id=:id"), {"id": org_id}) == 0
            assert await db.scalar(text(
                "select count(*) from password_reset_token where id=:id"
            ), {"id": reset_id}) == 0
            assert await db.scalar(text(
                "select status from org_purge_run where id=:id"
            ), {"id": run.id}) == "completed"
            assert await db.scalar(text(
                "select bundle_object_key from export_request where id=:id"
            ), {"id": export_id}) is None
        objects.keys.add(export_key)
        await reconcile_completed_purge_objects(
            org_id, run.id, history=history,
            object_store=objects,  # type: ignore[arg-type]
        )
        assert not objects.keys
        # Simulate restoring a database snapshot from before claim while the
        # independent lifecycle authority still contains every later decision.
        async with engine.begin() as db:
            await db.execute(text(
                "delete from org_purge_step where purge_run_id=:run"
            ), {"run": run.id})
            await db.execute(text(
                "delete from org_purge_run where id=:run"
            ), {"run": run.id})
            await db.execute(text(
                "delete from org_lifecycle_operation where org_id=:org"
            ), {"org": org_id})
            await db.execute(text(
                "insert into org(id,name,registered_domain,timezone,lifecycle_status,"
                "lifecycle_sequence,offboarding_id,offboarding_started_at,purge_eligible_at,"
                "retention_policy_reference) values "
                "(:id,'Purge fixture',:domain,'Africa/Cairo','offboarding',0,:episode,"
                ":started,:eligible,'org-default-90d:v1')"
            ), {
                "id": org_id, "domain": f"purge-{org_id}.example.test",
                "episode": episode_id, "started": instant - timedelta(days=91),
                "eligible": instant - timedelta(days=1),
            })
            await db.execute(text(
                "insert into account(id,org_id,team_id,email,display_name,role,"
                "password_hash,status) values "
                "(:manager,:org,:manager,:manager_email,'Manager','manager','x','active'),"
                "(:rep,:org,:manager,:rep_email,'Rep','rep','x','active')"
            ), {
                "manager": manager_id, "rep": rep_id, "org": org_id,
                "manager_email": f"manager-{manager_id}@example.test",
                "rep_email": f"rep-{rep_id}@example.test",
            })
            await db.execute(text(
                "insert into password_reset_token(id,account_id,token_hash,expires_at) "
                "values(:id,:account,:hash,:expires)"
            ), {
                "id": reset_id, "account": rep_id,
                "hash": str(reset_id), "expires": instant + timedelta(days=1),
            })
            await db.execute(text(
                "update export_request set bundle_object_key=:key where id=:id"
            ), {"id": export_id, "key": export_key})
        objects.keys.add(export_key)
        catalog, purged, _ = await _verify_lifecycle_restore(
            history=history, markers=[], ledger=None,  # type: ignore[arg-type]
            object_store=objects,  # type: ignore[arg-type]
            sessions=sessions,
        )
        assert org_id in catalog and org_id in purged
        async with engine.connect() as db:
            assert await db.scalar(text("select count(*) from org where id=:id"), {"id": org_id}) == 0
            assert await db.scalar(text("select count(*) from account where org_id=:id"), {"id": org_id}) == 0
            assert await db.scalar(text(
                "select status from org_purge_run where id=:id"
            ), {"id": run.id}) == "completed"
            assert await db.scalar(text(
                "select bundle_object_key from export_request where id=:id"
            ), {"id": export_id}) is None
        assert not objects.keys
    finally:
        async with engine.begin() as db:
            await db.execute(text("delete from export_request where id=:id"), {"id": export_id})
            await db.execute(text(
                "delete from password_reset_token where id=:id"
            ), {"id": reset_id})
            await db.execute(text("delete from account where id=:id"), {"id": rep_id})
            await db.execute(text("delete from account where id=:id"), {"id": manager_id})
            await db.execute(text(
                "delete from org_purge_step where purge_run_id in "
                "(select id from org_purge_run where org_id=:org)"
            ), {"org": org_id})
            await db.execute(text("delete from org_purge_run where org_id=:org"), {"org": org_id})
            await db.execute(text(
                "delete from org_lifecycle_operation where org_id=:org"
            ), {"org": org_id})
            await db.execute(text("delete from org where id=:org"), {"org": org_id})
        await valkey.aclose()
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.verifies("SEC-043")
async def test_restore_replays_term_accepted_after_database_snapshot() -> None:
    migration_url = os.environ.get("TEST_MIGRATION_URL")
    maintenance_url = os.environ.get("DATABASE_URL", "")
    if not migration_url or "bluelab_maintenance" not in maintenance_url:
        pytest.skip("isolated migration and maintenance database roles required")
    engine = create_async_engine(migration_url)
    org_id, actor_id, term_id, operation_id = (new_id() for _ in range(4))
    history = LifecycleHistory(_MemoryHistoryStore(), _MemoryHistoryStore())
    objects = _Objects(f"exports/{new_id()}.zip")
    objects.keys.clear()
    valkey = fakeredis.aioredis.FakeRedis(decode_responses=True)
    sessions = SessionStore(valkey, idle_seconds=3600, absolute_seconds=86400)
    try:
        await history.register(org_id)
        async with engine.begin() as db:
            await db.execute(text(
                "insert into org(id,name,registered_domain,timezone,lifecycle_sequence) "
                "values(:id,'Restore term fixture',:domain,'Africa/Cairo',0)"
            ), {"id": org_id, "domain": f"restore-term-{org_id}.example.test"})
            await db.execute(text(
                "insert into ops_account(id,email,display_name,password_hash,"
                "totp_secret_ciphertext) values(:id,:email,'Restore operator','x',:secret)"
            ), {"id": actor_id, "email": f"ops-{actor_id}@example.test", "secret": b"x"})
        accepted = datetime.now(UTC)
        payload = {
            "service_term_id": str(term_id), "start_on": "2026-09-17",
            "last_access_on": "2026-09-20",
            "starts_at": "2026-09-16T21:00:00+00:00",
            "ends_at": "2026-09-20T21:00:00+00:00",
            "calendar_timezone": "Africa/Cairo",
            "contract_reference": "restore-fixture-contract",
            "retention_policy": {
                "policy_reference": "org-default-90d:v1", "period_value": 90,
                "period_unit": "elapsed_days", "calendar_timezone": None,
            },
            "policy_is_revision": False,
            "projected_purge_eligible_at": "2026-12-19T21:00:00+00:00",
            "cancelled_offboarding_id": None,
            "elapsed_hold_started_at": None,
            "previous_service_end": None,
        }
        await history.append(
            org_id=org_id, operation_id=operation_id, expected_sequence=0,
            action="confirm_term", accepted_at=accepted.isoformat(),
            actor_id=actor_id, reason="restore accepted term", data=payload,
        )
        await replay_organization(
            org_id, await history.verified_events(org_id), history=history,
            markers=[], ledger=None,  # type: ignore[arg-type]
            object_store=objects,  # type: ignore[arg-type]
            sessions=sessions,
        )
        async with engine.connect() as db:
            state = (await db.execute(text(
                "select lifecycle_sequence,current_service_term_id,service_ends_at "
                "from org where id=:id"
            ), {"id": org_id})).one()
            assert state.lifecycle_sequence == 1
            assert state.current_service_term_id == term_id
            assert state.service_ends_at == datetime.fromisoformat(payload["ends_at"])
            assert await db.scalar(text(
                "select status from org_lifecycle_operation where id=:id"
            ), {"id": operation_id}) == "applied"
    finally:
        async with engine.begin() as db:
            await db.execute(text("delete from ops_audit where target_org_id=:org"), {"org": org_id})
            await db.execute(text(
                "update org set current_service_term_id=null,service_starts_at=null,"
                "service_ends_at=null,service_term_enforced=false where id=:org"
            ), {"org": org_id})
            await db.execute(text("delete from org_service_term where org_id=:org"), {"org": org_id})
            await db.execute(text(
                "delete from org_lifecycle_operation where org_id=:org"
            ), {"org": org_id})
            await db.execute(text("delete from org where id=:org"), {"org": org_id})
            await db.execute(text("delete from ops_account where id=:id"), {"id": actor_id})
        await valkey.aclose()
        await engine.dispose()
