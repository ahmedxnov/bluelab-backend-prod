"""The ordinary transaction boundary follows the current organization term."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from bluelab.calls.admission import require_call_service_access
from bluelab.platform.clock import service_term_bounds
from bluelab.platform.db.privileged import system_scope
from bluelab.platform.db.scope import Role, ScopeContext
from bluelab.platform.db.session import scoped_transaction
from bluelab.platform.errors import catalog
from bluelab.platform.errors.denial import ProblemError
from bluelab.platform.ids import new_id
from bluelab.platform.queue.catalog import Lane
from bluelab.platform.queue.compat import envelope
from bluelab.platform.queue.context import OrganizationWorkSuspended, job_transaction

pytestmark = [pytest.mark.l3_integration, pytest.mark.l7_security]


@pytest.mark.verifies("SEC-042")
async def test_term_and_pending_decision_fence_ordinary_transactions(auth_engine, world) -> None:
    scope = ScopeContext.account(
        org_id=world.org, team_id=world.manager, account_id=world.rep, role=Role.REP
    )
    async with async_sessionmaker(auth_engine)() as db, db.begin():
        await db.execute(
            text("update org set service_term_enforced=true where id=:org"),
            {"org": world.org},
        )

    with pytest.raises(ProblemError) as unconfigured:
        async with scoped_transaction(scope):
            pass
    assert unconfigured.value.problem is catalog.ORGANIZATION_SUSPENDED
    ordinary_job = envelope({"email_send_id": str(new_id())}, org_id=world.org)
    with pytest.raises(OrganizationWorkSuspended):
        async with job_transaction(Lane.DISPATCH_EMAIL, ordinary_job, job_id="test"):
            pass
    async with job_transaction(Lane.EXECUTE_EXPORT, ordinary_job, job_id="test") as (db, _):
        assert await db.scalar(text("select 1")) == 1

    term_id = new_id()
    moment = datetime.now(tz=UTC)
    future_start_on = moment.date() + timedelta(days=2)
    future_last_on = moment.date() + timedelta(days=3)
    future_start, future_end = service_term_bounds(future_start_on, future_last_on, "UTC")
    async with async_sessionmaker(auth_engine)() as db, db.begin():
        await db.execute(
            text(
                "insert into org_service_term (id,org_id,lifecycle_sequence,start_on,"
                "last_access_on,calendar_timezone,starts_at,ends_at,contract_reference,"
                "retention_policy_reference,confirmed_by,confirmed_at,reason) "
                "values (:id,:org,1,:start_on,:last_on,'UTC',:starts,:ends,"
                "'contract','org-default-90d:v1',:actor,:confirmed,'test')"
            ),
            {
                "id": term_id, "org": world.org, "start_on": future_start_on,
                "last_on": future_last_on, "starts": future_start, "ends": future_end,
                "actor": world.manager, "confirmed": moment,
            },
        )
        await db.execute(
            text("update org set current_service_term_id=:term, service_starts_at=:starts, "
                 "service_ends_at=:ends, lifecycle_sequence=1 where id=:org"),
            {"term": term_id, "starts": future_start,
             "ends": future_end, "org": world.org},
        )

    with pytest.raises(ProblemError) as scheduled:
        async with scoped_transaction(scope):
            pass
    assert scheduled.value.problem is catalog.ORGANIZATION_SUSPENDED

    active_id = new_id()
    active_start_on = moment.date() - timedelta(days=1)
    active_last_on = moment.date() + timedelta(days=1)
    active_start, active_end = service_term_bounds(active_start_on, active_last_on, "UTC")
    async with async_sessionmaker(auth_engine)() as db, db.begin():
        await db.execute(
            text(
                "insert into org_service_term (id,org_id,lifecycle_sequence,start_on,"
                "last_access_on,calendar_timezone,starts_at,ends_at,contract_reference,"
                "retention_policy_reference,confirmed_by,confirmed_at,reason) "
                "values (:id,:org,2,:start_on,:last_on,'UTC',:starts,:ends,"
                "'contract','org-default-90d:v1',:actor,:confirmed,'test')"
            ),
            {"id": active_id, "org": world.org, "start_on": active_start_on,
             "last_on": active_last_on, "starts": active_start, "ends": active_end,
             "actor": world.manager, "confirmed": moment},
        )
        await db.execute(
            text("update org set current_service_term_id=:term, service_starts_at=:starts, "
                 "service_ends_at=:ends, lifecycle_sequence=2 where id=:org"),
            {"term": active_id, "starts": active_start, "ends": active_end, "org": world.org},
        )
    async with scoped_transaction(scope) as db:
        assert await db.scalar(text("select 1")) == 1
        await require_call_service_access(db, world.org)
        with pytest.raises(ProblemError) as wrong_org:
            await require_call_service_access(db, new_id())
        assert wrong_org.value.problem is catalog.ORGANIZATION_SUSPENDED
    async with job_transaction(Lane.DISPATCH_EMAIL, ordinary_job, job_id="test") as (db, _):
        assert await db.scalar(text("select 1")) == 1
    async with scoped_transaction(system_scope(org_id=world.org)) as db:
        await require_call_service_access(db, world.org)

    operation_id = new_id()
    async with async_sessionmaker(auth_engine)() as db, db.begin():
        await db.execute(
            text("insert into org_lifecycle_operation "
                 "(id,org_id,action,expected_sequence,reason) "
                 "values (:id,:org,'renew_term',2,'test')"),
            {"id": operation_id, "org": world.org},
        )
    with pytest.raises(ProblemError) as pending:
        async with scoped_transaction(scope):
            pass
    assert pending.value.problem is catalog.ORGANIZATION_SUSPENDED
    with pytest.raises(OrganizationWorkSuspended):
        async with job_transaction(Lane.DISPATCH_EMAIL, ordinary_job, job_id="test"):
            pass
    async with scoped_transaction(system_scope(org_id=world.org)) as db:
        with pytest.raises(ProblemError) as call_pending:
            await require_call_service_access(db, world.org)
    assert call_pending.value.problem is catalog.ORGANIZATION_SUSPENDED
    async with async_sessionmaker(auth_engine)() as db, db.begin():
        await db.execute(
            text("delete from org_lifecycle_operation where id=:id"),
            {"id": operation_id},
        )


async def test_maintenance_helpers_refuse_ordinary_and_org_scoped_callers(world) -> None:
    with pytest.raises(DBAPIError):
        async with scoped_transaction(ScopeContext.anonymous()) as db:
            await db.execute(text("select app_retention_sweep('RC-3', now())"))
    with pytest.raises(DBAPIError):
        async with scoped_transaction(system_scope(org_id=world.org)) as db:
            await db.execute(text("select org_id from app_due_org_service_terms(now(), 10)"))


async def test_maintenance_guc_spoofing_cannot_invoke_global_definers() -> None:
    """The API role cannot borrow a worker's global maintenance authority."""
    for statement in (
        "select app_retention_sweep('RC-3', now())",
        "select app_stale_pending_recordings(now())",
        "select org_id from app_due_org_service_terms(now(), 10)",
        "select org_id from app_pending_org_term_operations(10)",
    ):
        with pytest.raises(DBAPIError) as denied:
            async with scoped_transaction(system_scope(org_id=UUID(int=0))) as db:
                await db.execute(text(statement))
        assert "permission denied for function" in str(denied.value)
