"""Subject-rights acceptance projects the independently ordered purge barrier."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker
from tests.l3_integration.operations.conftest import OPS_PASSWORD, OPS_SEED
from tests.l3_integration.operations.test_org_service_terms import MemoryStore

from bluelab.adapters.lifecycle_history import LifecycleHistory
from bluelab.adapters.object_store import PresignedObject
from bluelab.platform.ids import new_id
from bluelab.platform.security.totp import STEP_SECONDS, code_for_step
from bluelab.work.org_terms import recover_one

pytestmark = [pytest.mark.l3_integration, pytest.mark.l7_security]


async def test_subject_state_decision_releases_only_with_evidence(
    ops_client, ops_world, operations_engine, monkeypatch
) -> None:
    from bluelab.api import ops_v1

    history = LifecycleHistory(MemoryStore())
    monkeypatch.setattr(ops_v1, "create_lifecycle_history", lambda _settings: history)

    class UnarmedLedger:
        async def is_armed(self, _request_id):
            return False

    monkeypatch.setattr(ops_v1, "create_erasure_ledger", lambda _settings: UnarmedLedger())
    code = code_for_step(OPS_SEED, int(datetime.now(UTC).timestamp()) // STEP_SECONDS)
    signed_in = await ops_client.post(
        "/ops/v1/session",
        json={"email": ops_world.email, "password": OPS_PASSWORD, "totp_code": code},
    )
    assert signed_in.status_code == 200, signed_in.text
    org = await ops_client.post(
        "/ops/v1/orgs", json={"name": "State Org", "registered_domain": "example.com",
                              "reason": "signed contract"},
    )
    assert org.status_code == 201, org.text
    org_id = org.json()["org_id"]
    account = await ops_client.post(
        "/ops/v1/accounts",
        json={"org_id": org_id, "email": "manager@example.com",
              "display_name": "State Manager", "role": "manager",
              "reason": "approved roster"},
    )
    assert account.status_code == 201, account.text
    accepted = await ops_client.post(
        "/ops/v1/export-requests",
        json={"org_id": org_id, "subject_kind": "account",
              "subject_id": account.json()["account_id"], "reason": "subject request"},
        headers={"Lifecycle-Operation-Id": str(new_id())},
    )
    assert accepted.status_code == 202, accepted.text
    request_id = UUID(accepted.json()["id"])
    path = f"/ops/v1/export-requests/{request_id}/state"
    wait_op = new_id()
    waiting = await ops_client.post(
        path, json={"expected_status": "pending", "status": "awaiting_input",
                    "reason": "identity verification needed"},
        headers={"Lifecycle-Operation-Id": str(wait_op)},
    )
    assert waiting.status_code == 200, waiting.text
    assert waiting.json()["status"] == "awaiting_input"
    assert datetime.fromisoformat(waiting.json()["response_due_at"]) > datetime.now(UTC)
    replay = await ops_client.post(
        path, json={"expected_status": "pending", "status": "awaiting_input",
                    "reason": "identity verification needed"},
        headers={"Lifecycle-Operation-Id": str(wait_op)},
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["response_due_at"] == waiting.json()["response_due_at"]
    no_evidence = await ops_client.post(
        path, json={"expected_status": "awaiting_input", "status": "withdrawn",
                    "reason": "authenticated requester withdrew"},
        headers={"Lifecycle-Operation-Id": str(new_id())},
    )
    assert no_evidence.status_code == 422
    closure = await ops_client.post(
        path, json={"expected_status": "awaiting_input", "status": "withdrawn",
                    "reason": "authenticated requester withdrew",
                    "evidence_reference": "rights-case-123"},
        headers={"Lifecycle-Operation-Id": str(new_id())},
    )
    assert closure.status_code == 200, closure.text
    assert closure.json()["status"] == "withdrawn"
    assert closure.json()["response_due_at"] is None
    maker = async_sessionmaker(operations_engine, expire_on_commit=False)
    async with maker() as db:
        assert await db.scalar(text(
            "select status from org_deletion_restriction where related_request_id=:id"
        ), {"id": request_id}) == "released"
        assert await db.scalar(text(
            "select count(*) from ops_audit where target_org_id=:org "
            "and verb='resolve_subject_request'"
        ), {"org": UUID(org_id)}) == 2
    assert (await history.verified_head(UUID(org_id))).sequence == 3


async def test_export_acceptance_is_atomic_with_restriction_audit_and_job(
    ops_client, ops_world, operations_engine, monkeypatch
) -> None:
    from bluelab.api import ops_v1

    history = LifecycleHistory(MemoryStore())
    monkeypatch.setattr(ops_v1, "create_lifecycle_history", lambda _settings: history)
    code = code_for_step(OPS_SEED, int(datetime.now(UTC).timestamp()) // STEP_SECONDS)
    signed_in = await ops_client.post(
        "/ops/v1/session",
        json={"email": ops_world.email, "password": OPS_PASSWORD, "totp_code": code},
    )
    assert signed_in.status_code == 200, signed_in.text
    org = await ops_client.post(
        "/ops/v1/orgs",
        json={"name": "Rights Org", "registered_domain": "example.com",
              "reason": "signed contract"},
    )
    assert org.status_code == 201, org.text
    org_id = org.json()["org_id"]
    account = await ops_client.post(
        "/ops/v1/accounts",
        json={"org_id": org_id, "email": "manager@example.com",
              "display_name": "Rights Manager", "role": "manager",
              "reason": "approved roster"},
    )
    assert account.status_code == 201, account.text
    operation_id = new_id()
    payload = {"org_id": org_id, "subject_kind": "account",
               "subject_id": account.json()["account_id"], "reason": "subject request"}
    path = "/ops/v1/export-requests"
    accepted = await ops_client.post(
        path, json=payload, headers={"Lifecycle-Operation-Id": str(operation_id)}
    )
    assert accepted.status_code == 202, accepted.text
    request_id = UUID(accepted.json()["id"])
    replay = await ops_client.post(
        path, json=payload, headers={"Lifecycle-Operation-Id": str(operation_id)}
    )
    assert replay.status_code == 202, replay.text
    assert replay.json()["id"] == str(request_id)
    lifecycle = await ops_client.get(f"/ops/v1/orgs/{org_id}")
    assert lifecycle.status_code == 200, lifecycle.text
    assert len(lifecycle.json()["restrictions"]) == 1
    assert lifecycle.json()["restrictions"][0]["related_request_id"] == str(request_id)
    audit_page = await ops_client.get("/ops/v1/audit", params={"org_id": org_id})
    assert audit_page.status_code == 200, audit_page.text

    maker = async_sessionmaker(operations_engine, expire_on_commit=False)
    async with maker() as db:
        restriction = (await db.execute(text(
            "select id,status,offboarding_id from org_deletion_restriction "
            "where org_id=:org and related_request_id=:request"
        ), {"org": UUID(org_id), "request": request_id})).mappings().one()
        assert restriction["status"] == "active"
        assert restriction["offboarding_id"] is None
        assert await db.scalar(text(
            "select count(*) from export_request where id=:request "
            "and restriction_id=:restriction and status='pending'"
        ), {"request": request_id, "restriction": restriction["id"]}) == 1
        assert await db.scalar(text(
            "select count(*) from ops_audit where target_org_id=:org "
            "and verb='execute_export' and target_ref->>'request_id'=:request"
        ), {"org": UUID(org_id), "request": str(request_id)}) == 1
        assert await db.scalar(text(
            "select count(*) from procrastinate_jobs where "
            "args #>> '{args,request_id}'=:request"
        ), {"request": str(request_id)}) == 1
    assert (await history.verified_head(UUID(org_id))).sequence == 1

    async with maker() as db, db.begin():
        await db.execute(text(
            "update export_request set status='ready', "
            "bundle_object_key=:key, expires_at=now()+interval '7 days' "
            "where id=:request"
        ), {"request": request_id, "key": f"exports/{request_id}.zip"})

    class Presigner:
        async def presign_get(self, authorization):
            assert authorization.object_ref.key == f"exports/{request_id}.zip"
            assert authorization.authorized_until <= datetime.now(UTC) + timedelta(hours=1)
            return PresignedObject("https://objects.test/one-bundle", datetime.now(UTC) + timedelta(minutes=5))

    monkeypatch.setattr("bluelab.api.deps.create_object_store", lambda _settings: Presigner())
    grant = await ops_client.get(f"{path}/{request_id}")
    assert grant.status_code == 200, grant.text
    assert grant.json()["status"] == "ready"
    assert grant.json()["bundle_url"] == "https://objects.test/one-bundle"
    async with maker() as db:
        audits = (await db.execute(text(
            "select target_ref from ops_audit where target_org_id=:org "
            "and verb='execute_export' order by occurred_at"
        ), {"org": UUID(org_id)})).scalars().all()
        assert len(audits) == 2
        assert any(item.get("delivery_expires_at") for item in audits)

    deactivated = await ops_client.post(
        f"/ops/v1/accounts/{account.json()['account_id']}/deactivate",
        json={"reason": "subject erasure prerequisite"},
    )
    assert deactivated.status_code == 200, deactivated.text
    erasure = await ops_client.post(
        "/ops/v1/erasure-requests", json=payload,
        headers={"Lifecycle-Operation-Id": str(new_id())},
    )
    assert erasure.status_code == 202, erasure.text
    assert erasure.json()["status"] == "pending"
    async with maker() as db:
        assert await db.scalar(text(
            "select count(*) from org_deletion_restriction "
            "where org_id=:org and status='active'"
        ), {"org": UUID(org_id)}) == 2
        assert await db.scalar(text(
            "select count(*) from erasure_request "
            "where id=:request and restriction_id is not null"
        ), {"request": UUID(erasure.json()["id"])}) == 1

    async with maker() as db, db.begin():
        await db.execute(text(
            "update org_deletion_restriction set status='released', "
            "released_at=now(), released_by=:actor, "
            "release_reason='completed delivery', "
            "release_evidence_reference='subject-request-test' "
            "where related_request_id=:request"
        ), {"actor": ops_world.ops_account_id, "request": request_id})
    closed_delivery = await ops_client.get(f"{path}/{request_id}")
    assert closed_delivery.status_code == 200, closed_delivery.text
    assert closed_delivery.json()["bundle_url"] is None


async def test_accepted_history_reconstructs_restriction_after_projection_failure(
    ops_client, ops_world, operations_engine, monkeypatch
) -> None:
    from bluelab.api import ops_v1
    from bluelab.lifecycle import subject_requests

    history = LifecycleHistory(MemoryStore())
    monkeypatch.setattr(ops_v1, "create_lifecycle_history", lambda _settings: history)
    code = code_for_step(OPS_SEED, int(datetime.now(UTC).timestamp()) // STEP_SECONDS)
    signed_in = await ops_client.post(
        "/ops/v1/session",
        json={"email": ops_world.email, "password": OPS_PASSWORD, "totp_code": code},
    )
    assert signed_in.status_code == 200, signed_in.text
    org = await ops_client.post(
        "/ops/v1/orgs",
        json={"name": "Replay Org", "registered_domain": "example.com",
              "reason": "signed contract"},
    )
    assert org.status_code == 201, org.text
    org_id = org.json()["org_id"]
    account = await ops_client.post(
        "/ops/v1/accounts",
        json={"org_id": org_id, "email": "manager@example.com",
              "display_name": "Replay Manager", "role": "manager",
              "reason": "approved roster"},
    )
    assert account.status_code == 201, account.text
    operation_id = new_id()
    payload = {"org_id": org_id, "subject_kind": "account",
               "subject_id": account.json()["account_id"], "reason": "subject request"}
    original_apply = subject_requests.apply

    async def failed_apply(*_args, **_kwargs):
        raise RuntimeError("simulated projection failure")

    monkeypatch.setattr(subject_requests, "apply", failed_apply)
    with pytest.raises(RuntimeError, match="simulated projection failure"):
        await ops_client.post(
            "/ops/v1/export-requests", json=payload,
            headers={"Lifecycle-Operation-Id": str(operation_id)},
        )
    monkeypatch.setattr(subject_requests, "apply", original_apply)
    assert (await history.verified_head(UUID(org_id))).sequence == 1
    maker = async_sessionmaker(operations_engine, expire_on_commit=False)
    async with maker() as db:
        assert await db.scalar(text(
            "select status from org_lifecycle_operation where id=:id"
        ), {"id": operation_id}) == "pending"
        assert await db.scalar(text(
            "select count(*) from org_deletion_restriction where org_id=:org"
        ), {"org": UUID(org_id)}) == 0
    async def no_calls(_org_id: UUID, _since: datetime) -> None:
        pass

    assert await recover_one(UUID(org_id), history, quiesce=no_calls)
    accepted = await ops_client.post(
        "/ops/v1/export-requests", json=payload,
        headers={"Lifecycle-Operation-Id": str(operation_id)},
    )
    assert accepted.status_code == 202, accepted.text
    async with maker() as db:
        request_id = UUID(accepted.json()["id"])
        assert await db.scalar(text(
            "select count(*) from org_deletion_restriction "
            "where org_id=:org and related_request_id=:request and status='active'"
        ), {"org": UUID(org_id), "request": request_id}) == 1
        assert await db.scalar(text(
            "select count(*) from procrastinate_jobs where "
            "args #>> '{args,request_id}'=:request"
        ), {"request": str(request_id)}) == 1
