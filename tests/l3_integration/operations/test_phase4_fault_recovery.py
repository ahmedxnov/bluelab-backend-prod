"""Phase 4 content-free fault listing and audited identity-only re-drive."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker
from tests.l3_integration.operations.conftest import OPS_PASSWORD, OPS_SEED

from bluelab.adapters.object_store import ObjectRef
from bluelab.platform.ids import new_id
from bluelab.platform.security.totp import STEP_SECONDS, code_for_step

pytestmark = [
    pytest.mark.l3_integration,
    pytest.mark.l7_security,
    pytest.mark.invariant_path,
]


def _code() -> str:
    step = int(datetime.now(UTC).timestamp()) // STEP_SECONDS
    return code_for_step(OPS_SEED, step)


async def _sign_in(client, world) -> None:
    response = await client.post(
        "/ops/v1/session",
        json={
            "email": world.email,
            "password": OPS_PASSWORD,
            "totp_code": _code(),
        },
    )
    assert response.status_code == 200


async def _seed_fault_world(client, engine) -> dict[str, UUID]:
    domain = f"phase4-{new_id()}.example"
    org_response = await client.post(
        "/ops/v1/orgs",
        json={
            "name": "Phase Four Fault Org",
            "registered_domain": domain,
            "timezone": "Africa/Cairo",
            "reason": "Phase 4 fault recovery fixture",
        },
    )
    assert org_response.status_code == 201
    org_id = UUID(org_response.json()["org_id"])
    manager_response = await client.post(
        "/ops/v1/accounts",
        json={
            "org_id": str(org_id),
            "email": f"manager-{new_id()}@{domain}",
            "display_name": "Phase Four Manager",
            "role": "manager",
            "reason": "Phase 4 fault recovery fixture",
        },
    )
    assert manager_response.status_code == 201
    manager_id = UUID(manager_response.json()["account_id"])
    rep_response = await client.post(
        "/ops/v1/accounts",
        json={
            "org_id": str(org_id),
            "email": f"rep-{new_id()}@{domain}",
            "display_name": "Phase Four Rep",
            "role": "rep",
            "manager_account_id": str(manager_id),
            "reason": "Phase 4 fault recovery fixture",
        },
    )
    assert rep_response.status_code == 201
    rep_id = UUID(rep_response.json()["account_id"])
    ids = {
        "org": org_id,
        "manager": manager_id,
        "rep": rep_id,
        "drill": new_id(),
        "grading_attempt": new_id(),
        "playback_attempt": new_id(),
        "grading_fault": new_id(),
        "playback_fault": new_id(),
    }
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db, db.begin():
        await db.execute(
            text(
                "insert into drill (id,org_id,team_id,author_account_id,self_authored,"
                "status,call_type,lead_type)"
                " values (:drill,:org,:manager,:manager,false,'draft','discovery',"
                "'referral')"
            ),
            ids,
        )
        await db.execute(
            text(
                "insert into drill_concealed (drill_id,org_id,team_id,challenges,hidden_motives)"
                " values (:drill,:org,:manager,'[]','[]')"
            ),
            ids,
        )
        for key, recording_status in (
            ("grading_attempt", "none"),
            ("playback_attempt", "pending"),
        ):
            await db.execute(
                text(
                    "insert into attempt (id,org_id,team_id,drill_id,rep_account_id,"
                    "self_authored,status,recording_status)"
                    " values (:attempt,:org,:manager,:drill,:rep,false,'grading_pending',"
                    ":recording_status)"
                ),
                {
                    **ids,
                    "attempt": ids[key],
                    "recording_status": recording_status,
                },
            )
        await db.execute(
            text(
                "insert into ops_fault (id,org_id,kind,attempt_id,detail) values"
                " (:grading_fault,:org,'grading_failure',:grading_attempt,"
                "  cast(:grading_detail as jsonb)),"
                " (:playback_fault,:org,'playback_asset',:playback_attempt,"
                "  cast(:playback_detail as jsonb))"
            ),
            {
                **ids,
                "grading_detail": json.dumps(
                    {
                        "error_class": "grading_retry_exhausted",
                        "retry_count": 5,
                        "transcript": "must never leave storage",
                    }
                ),
                "playback_detail": json.dumps(
                    {"error_class": "object_missing", "retry_count": 1}
                ),
            },
        )
    return ids


async def test_restore_missing_recording_opens_identity_only_fault(
    ops_client, ops_world, operations_engine,
) -> None:
    await _sign_in(ops_client, ops_world)
    ids = await _seed_fault_world(ops_client, operations_engine)
    key = ObjectRef.recording(
        org_id=ids["org"], attempt_id=ids["grading_attempt"],
    ).key
    maker = async_sessionmaker(operations_engine, expire_on_commit=False)
    async with maker() as db, db.begin():
        await db.execute(text(
            "update attempt set recording_status='available',recording_object_key=:key "
            "where id=:attempt"
        ), {"key": key, "attempt": ids["grading_attempt"]})
        category = await db.scalar(text(
            "select app_mark_missing_object(:key)"
        ), {"key": key})
        assert category == "recording"
        assert await db.scalar(text(
            "select recording_status from attempt where id=:attempt"
        ), {"attempt": ids["grading_attempt"]}) == "unavailable"
        assert await db.scalar(text(
            "select count(*) from ops_fault where attempt_id=:attempt "
            "and kind='playback_asset' and status='open'"
        ), {"attempt": ids["grading_attempt"]}) == 1


@pytest.mark.verifies("FR-SCR-009", "FR-SCR-013", "SEC-040")
async def test_faults_are_content_free_and_redrive_only_named_identity(
    ops_client,
    ops_world,
    operations_engine,
    monkeypatch,
) -> None:
    await _sign_in(ops_client, ops_world)
    ids = await _seed_fault_world(ops_client, operations_engine)

    listed = await ops_client.get("/ops/v1/faults", params={"status": "open"})
    assert listed.status_code == 200
    by_id = {item["id"]: item for item in listed.json()["data"]}
    grading = by_id[str(ids["grading_fault"])]
    assert grading["detail"] == {
        "error_class": "grading_retry_exhausted",
        "retry_count": 5,
    }
    assert "transcript" not in listed.text

    def object_store_must_not_be_built(_settings):
        raise AssertionError("grading re-drive touched the object store")

    monkeypatch.setattr(
        "bluelab.api.deps.create_object_store", object_store_must_not_be_built
    )
    resolved = await ops_client.post(
        f"/ops/v1/faults/{ids['grading_fault']}/resolve",
        json={"reason": "Retry the completed call after evaluator recovery"},
    )
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "resolved"

    replay = await ops_client.post(
        f"/ops/v1/faults/{ids['grading_fault']}/resolve",
        json={"reason": "Must not enqueue a duplicate"},
    )
    assert replay.status_code == 409
    assert replay.json()["type"].endswith("/fault-already-resolved")

    class MissingObjectStore:
        async def exists(self, ref: ObjectRef) -> bool:
            assert ref == ObjectRef.recording(
                org_id=ids["org"], attempt_id=ids["playback_attempt"]
            )
            return False

    monkeypatch.setattr(
        "bluelab.api.deps.create_object_store", lambda _settings: MissingObjectStore()
    )
    playback = await ops_client.post(
        f"/ops/v1/faults/{ids['playback_fault']}/resolve",
        json={"reason": "Recheck the exact recording object"},
    )
    assert playback.status_code == 200
    assert playback.json()["status"] == "resolved"

    maker = async_sessionmaker(operations_engine, expire_on_commit=False)
    async with maker() as db:
        jobs = (
            await db.execute(
                text(
                    "select args from procrastinate_jobs"
                    " where args #>> '{args,attempt_id}' = :attempt"
                ),
                {"attempt": str(ids["grading_attempt"])},
            )
        ).scalars().all()
        audits = (
            await db.execute(
                text(
                    "select target_ref,reason from ops_audit"
                    " where ops_account_id=:actor and verb='resolve_fault'"
                    " order by occurred_at"
                ),
                {"actor": ops_world.ops_account_id},
            )
        ).mappings().all()
        recording_status = (
            await db.execute(
                text("select recording_status from attempt where id=:attempt"),
                {"attempt": ids["playback_attempt"]},
            )
        ).scalar_one()

    assert len(jobs) == 1
    assert jobs[0]["args"] == {"attempt_id": str(ids["grading_attempt"])}
    assert len(audits) == 2
    assert all(
        set(item["target_ref"]) == {"fault_id", "attempt_id", "kind", "redrive"}
        for item in audits
    )
    assert all(item["reason"] for item in audits)
    assert recording_status == "unavailable"
