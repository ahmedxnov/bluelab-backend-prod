"""Phase 9 lifecycle, fence, residue, and replay invariants."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from bluelab.adapters.erasure_ledger import ErasureMarker
from bluelab.adapters.object_store import (
    AuthorizedObjectRead,
    DeletionReason,
    ObjectRef,
)
from bluelab.entrypoints.restore_reconcile import ensure_replay_pending
from bluelab.modules.drills.service import archive_drill
from bluelab.modules.hiring.service import replace_assessment
from bluelab.modules.operations.retention import sweep, sweep_stale_recordings
from bluelab.modules.operations.subject_rights import execute_erasure
from bluelab.modules.training.assignment import put_assignment
from bluelab.platform.errors.denial import ProblemError
from bluelab.platform.ids import new_id


class MemoryLedger:
    def __init__(self) -> None:
        self.armed: dict[UUID, bytes] = {}

    async def arm(self, marker: ErasureMarker) -> None:
        encoded = marker.bytes()
        existing = self.armed.setdefault(marker.request_id, encoded)
        if existing != encoded:
            # armed_at differs on replay; the immutable first marker remains authority.
            return

    async def markers(self) -> list[ErasureMarker]:
        return []


class MemoryObjects:
    def __init__(self, values: dict[str, bytes]) -> None:
        self.values = values

    async def put(self, ref: ObjectRef, data: bytes, *, content_type: str) -> None:
        self.values[ref.key] = data

    async def get(self, ref: ObjectRef) -> bytes:
        return self.values[ref.key]

    async def exists(self, ref: ObjectRef) -> bool:
        return ref.key in self.values

    async def delete(self, ref: ObjectRef, *, reason: DeletionReason) -> None:
        assert reason in {DeletionReason.ERASURE, DeletionReason.SWEEP}
        self.values.pop(ref.key, None)

    async def presign_get(self, authorization: AuthorizedObjectRead) -> Any:
        raise AssertionError("not used")


class FailingObjects(MemoryObjects):
    async def delete(self, ref: ObjectRef, *, reason: DeletionReason) -> None:
        raise RuntimeError("simulated object deletion failure")


@pytest.mark.asyncio
async def test_archive_withdraws_future_use_but_preserves_attempt_history(
    session, base_org, make_drill
) -> None:
    drill = await make_drill(status="published")
    rep, assignment, attempt = new_id(), new_id(), new_id()
    async with session.begin():
        await session.execute(
            text(
                "insert into account(id,org_id,team_id,email,display_name,role,password_hash) "
                "values(:id,:org,:team,:email,'Rep','rep','x')"
            ),
            {"id": rep, "org": base_org["org"], "team": base_org["manager"], "email": f"p9-{rep}@t.test"},
        )
        await session.execute(
            text(
                "insert into assignment(id,org_id,team_id,drill_id,due_date,attempts_allowed,created_by) "
                "values(:id,:org,:team,:drill,current_date,1,:team)"
            ),
            {"id": assignment, "org": base_org["org"], "team": base_org["manager"], "drill": drill},
        )
        await session.execute(
            text(
                "insert into assignment_recipient(assignment_id,org_id,team_id,rep_account_id) "
                "values(:assignment,:org,:team,:rep)"
            ),
            {"assignment": assignment, "org": base_org["org"], "team": base_org["manager"], "rep": rep},
        )
        await session.execute(
            text(
                "insert into attempt(id,org_id,team_id,drill_id,rep_account_id,self_authored,status) "
                "values(:id,:org,:team,:drill,:rep,false,'interrupted')"
            ),
            {"id": attempt, "org": base_org["org"], "team": base_org["manager"], "drill": drill, "rep": rep},
        )
        await archive_drill(
            session,
            drill_id=drill,
            org_id=base_org["org"],
            team_id=base_org["manager"],
            viewer_id=base_org["manager"],
        )
    async with session.begin():
        assert await session.scalar(text("select status from drill where id=:id"), {"id": drill}) == "archived"
        assert not await session.scalar(text("select exists(select 1 from assignment where id=:id)"), {"id": assignment})
        assert await session.scalar(text("select exists(select 1 from attempt where id=:id)"), {"id": attempt})


@pytest.mark.asyncio
async def test_archive_serializes_with_assignment_and_assessment_creation(
    session, engine, base_org, make_drill
) -> None:
    drill = await make_drill(status="published")
    rep, position = new_id(), new_id()
    async with session.begin():
        await session.execute(
            text(
                "insert into account(id,org_id,team_id,email,display_name,role,password_hash) "
                "values(:id,:org,:team,:email,'Rep','rep','x')"
            ),
            {
                "id": rep,
                "org": base_org["org"],
                "team": base_org["manager"],
                "email": f"p9-{rep}@t.test",
            },
        )
        await session.execute(
            text(
                "insert into position(id,org_id,team_id,title,openings) "
                "values(:id,:org,:team,'AE',1)"
            ),
            {"id": position, "org": base_org["org"], "team": base_org["manager"]},
        )

    maker = async_sessionmaker(engine, expire_on_commit=False)
    started = asyncio.Event()

    async def add_assignment() -> None:
        async with maker() as other, other.begin():
            started.set()
            await put_assignment(
                other,
                manager_account_id=base_org["manager"],
                org_id=base_org["org"],
                team_id=base_org["manager"],
                drill_id=drill,
                recipient_account_ids=[rep],
                due_date=datetime.now(UTC).date(),
                attempts_allowed=1,
            )

    async def add_assessment() -> None:
        async with maker() as other, other.begin():
            await replace_assessment(other, position_id=position, drill_ids=[drill])

    async with session.begin():
        await archive_drill(
            session,
            drill_id=drill,
            org_id=base_org["org"],
            team_id=base_org["manager"],
            viewer_id=base_org["manager"],
        )
        assignment_task = asyncio.create_task(add_assignment())
        assessment_task = asyncio.create_task(add_assessment())
        await started.wait()
        await asyncio.sleep(0.05)
        assert not assignment_task.done()
        assert not assessment_task.done()
    for task in (assignment_task, assessment_task):
        with pytest.raises(ProblemError):
            await asyncio.wait_for(task, timeout=5)
    async with session.begin():
        assert not await session.scalar(
            text("select exists(select 1 from assignment where drill_id=:drill)"),
            {"drill": drill},
        )
        assert not await session.scalar(
            text("select exists(select 1 from assessment_stage where drill_id=:drill)"),
            {"drill": drill},
        )


@pytest.mark.asyncio
async def test_manager_deactivation_blocks_until_dependents_move(session, base_org) -> None:
    manager, rep, position = new_id(), new_id(), new_id()
    async with session.begin():
        await session.execute(text("insert into account(id,org_id,team_id,email,display_name,role,password_hash) values(:id,:org,:id,:email,'Owner','manager','x')"), {"id": manager, "org": base_org["org"], "email": f"p9-{manager}@t.test"})
        await session.execute(text("insert into account(id,org_id,team_id,email,display_name,role,password_hash) values(:id,:org,:team,:email,'Rep','rep','x')"), {"id": rep, "org": base_org["org"], "team": manager, "email": f"p9-{rep}@t.test"})
        await session.execute(text("insert into position(id,org_id,team_id,title,openings,status) values(:id,:org,:team,'AE',1,'active')"), {"id": position, "org": base_org["org"], "team": manager})
        blocked = (await session.execute(text("select * from app_deactivate_account(:id)"), {"id": manager})).mappings().one()
        assert (blocked["dependent_reps"], blocked["open_positions"]) == (1, 1)
        await session.execute(text("select app_change_team(:rep,:manager)"), {"rep": rep, "manager": base_org["manager"]})
        await session.execute(text("select app_transfer_position(:position,:manager)"), {"position": position, "manager": base_org["manager"]})
        ready = (await session.execute(text("select * from app_deactivate_account(:id)"), {"id": manager})).mappings().one()
        assert (ready["dependent_reps"], ready["open_positions"]) == (0, 0)
        assert await session.scalar(text("select status from account where id=:id"), {"id": manager}) == "deactivated"


@pytest.mark.asyncio
async def test_deactivation_serializes_with_incoming_reassignment(
    session, engine, base_org
) -> None:
    manager, rep = new_id(), new_id()
    async with session.begin():
        await session.execute(
            text(
                "insert into account(id,org_id,team_id,email,display_name,role,password_hash) "
                "values(:id,:org,:id,:email,'Destination','manager','x')"
            ),
            {"id": manager, "org": base_org["org"], "email": f"p9-{manager}@t.test"},
        )
        await session.execute(
            text(
                "insert into account(id,org_id,team_id,email,display_name,role,password_hash) "
                "values(:id,:org,:team,:email,'Rep','rep','x')"
            ),
            {
                "id": rep,
                "org": base_org["org"],
                "team": base_org["manager"],
                "email": f"p9-{rep}@t.test",
            },
        )

    maker = async_sessionmaker(engine, expire_on_commit=False)
    started = asyncio.Event()

    async def incoming_change() -> object:
        async with maker() as other, other.begin():
            started.set()
            return (
                await other.execute(
                    text("select * from app_change_team(:rep,:manager)"),
                    {"rep": rep, "manager": manager},
                )
            ).one_or_none()

    async with session.begin():
        await session.execute(
            text("select * from app_deactivate_account(:manager)"),
            {"manager": manager},
        )
        change = asyncio.create_task(incoming_change())
        await started.wait()
        await asyncio.sleep(0.05)
        assert not change.done()
    assert await asyncio.wait_for(change, timeout=5) is None
    async with session.begin():
        assert await session.scalar(
            text("select team_id from account where id=:rep"), {"rep": rep}
        ) == base_org["manager"]


@pytest.mark.asyncio
async def test_deactivation_serializes_with_incoming_position_transfer(
    session, engine, base_org
) -> None:
    manager, position = new_id(), new_id()
    async with session.begin():
        await session.execute(
            text(
                "insert into account(id,org_id,team_id,email,display_name,role,password_hash) "
                "values(:id,:org,:id,:email,'Destination','manager','x')"
            ),
            {"id": manager, "org": base_org["org"], "email": f"p9-{manager}@t.test"},
        )
        await session.execute(
            text(
                "insert into position(id,org_id,team_id,title,openings,status) "
                "values(:id,:org,:team,'AE',1,'active')"
            ),
            {"id": position, "org": base_org["org"], "team": base_org["manager"]},
        )

    maker = async_sessionmaker(engine, expire_on_commit=False)
    started = asyncio.Event()

    async def incoming_transfer() -> object:
        async with maker() as other, other.begin():
            started.set()
            return (
                await other.execute(
                    text("select * from app_transfer_position(:position,:manager)"),
                    {"position": position, "manager": manager},
                )
            ).one_or_none()

    async with session.begin():
        await session.execute(
            text("select * from app_deactivate_account(:manager)"),
            {"manager": manager},
        )
        transfer = asyncio.create_task(incoming_transfer())
        await started.wait()
        await asyncio.sleep(0.05)
        assert not transfer.done()
    assert await asyncio.wait_for(transfer, timeout=5) is None
    async with session.begin():
        assert await session.scalar(
            text("select team_id from position where id=:position"),
            {"position": position},
        ) == base_org["manager"]


@pytest.mark.asyncio
async def test_erasure_deletes_personal_content_and_preserves_score_residue(
    session, base_org, make_drill
) -> None:
    drill = await make_drill(status="published")
    position, candidate, attempt, scorecard, request, operator = (new_id() for _ in range(6))
    recording = ObjectRef.recording(org_id=base_org["org"], attempt_id=attempt)
    report = ObjectRef.report(org_id=base_org["org"], candidate_id=candidate)
    ledger = MemoryLedger()
    objects = MemoryObjects({recording.key: b"audio", report.key: b"pdf"})
    async with session.begin():
        await session.execute(text("insert into ops_account(id,email,display_name,password_hash,totp_secret_ciphertext) values(:id,:email,'Ops','x',decode('00','hex'))"), {"id": operator, "email": f"p9-{operator}@t.test"})
        await session.execute(text("insert into position(id,org_id,team_id,title,openings,status) values(:id,:org,:team,'AE',1,'active')"), {"id": position, "org": base_org["org"], "team": base_org["manager"]})
        await session.execute(text("insert into candidate(id,org_id,team_id,position_id,name,email,phone,source,internal_note,preflight) values(:id,:org,:team,:position,'Person',:email,'1','referral','private',cast(:preflight as jsonb))"), {"id": candidate, "org": base_org["org"], "team": base_org["manager"], "position": position, "email": f"p9-{candidate}@t.test", "preflight": '{"passed":true}'})
        await session.execute(text("insert into assessment_stage(id,org_id,team_id,position_id,ord,drill_id) values(:id,:org,:team,:position,1,:drill)"), {"id": new_id(), "org": base_org["org"], "team": base_org["manager"], "position": position, "drill": drill})
        stage = await session.scalar(text("select id from assessment_stage where position_id=:position"), {"position": position})
        await session.execute(text("insert into attempt(id,org_id,team_id,drill_id,candidate_id,assessment_stage_id,self_authored,status,recording_object_key,recording_status) values(:id,:org,:team,:drill,:candidate,:stage,false,'in_progress',:recording,'available')"), {"id": attempt, "org": base_org["org"], "team": base_org["manager"], "drill": drill, "candidate": candidate, "stage": stage, "recording": recording.key})
        await session.execute(text("insert into transcript_entry(attempt_id,seq,org_id,team_id,speaker,at_ms,text) values(:attempt,0,:org,:team,'participant',0,'personal words')"), {"attempt": attempt, "org": base_org["org"], "team": base_org["manager"]})
        await session.execute(text("update attempt set status='grading_pending' where id=:id"), {"id": attempt})
        await session.execute(text("insert into scorecard(id,org_id,team_id,attempt_id,overall_score,takeaway) values(:id,:org,:team,:attempt,7.5,'personal summary')"), {"id": scorecard, "org": base_org["org"], "team": base_org["manager"], "attempt": attempt})
        await session.execute(text("insert into candidate_report(candidate_id,org_id,team_id,takeaway,pdf_object_key,pdf_status) values(:candidate,:org,:team,'personal report',:pdf,'available')"), {"candidate": candidate, "org": base_org["org"], "team": base_org["manager"], "pdf": report.key})
        await session.execute(text("insert into candidate_token(id,org_id,team_id,candidate_id,token_hash,expires_at) values(:id,:org,:team,:candidate,:hash,now()+interval '1 day')"), {"id": new_id(), "org": base_org["org"], "team": base_org["manager"], "candidate": candidate, "hash": str(new_id())})
        await session.execute(text("insert into consent_record(id,org_id,candidate_id,notice_version) values(:id,:org,:candidate,'v1')"), {"id": new_id(), "org": base_org["org"], "candidate": candidate})
        await session.execute(text("insert into terms_acceptance(id,org_id,candidate_id,terms_version,privacy_version) values(:id,:org,:candidate,'v1','v1')"), {"id": new_id(), "org": base_org["org"], "candidate": candidate})
        await session.execute(text("insert into erasure_request(id,org_id,subject_kind,subject_id,executed_by) values(:id,:org,'candidate',:candidate,:operator)"), {"id": request, "org": base_org["org"], "candidate": candidate, "operator": operator})
        await execute_erasure(session, request_id=request, ledger=ledger, object_store=objects)
    async with session.begin():
        candidate_row = (await session.execute(text("select * from candidate where id=:id"), {"id": candidate})).mappings().one()
        assert candidate_row["name"] == "Erased person"
        assert candidate_row["internal_note"] is None
        assert not await session.scalar(text("select exists(select 1 from transcript_entry where attempt_id=:id)"), {"id": attempt})
        assert await session.scalar(text("select overall_score from scorecard where id=:id"), {"id": scorecard}) == 7.5
        assert await session.scalar(text("select takeaway from scorecard where id=:id"), {"id": scorecard}) is None
        evidence = await session.scalar(text("select evidence from erasure_request where id=:id"), {"id": request})
        assert evidence["transcripts_deleted"] == 1
        assert evidence["objects_deleted"] == 2
        assert "object_keys" not in evidence
        assert objects.values == {}


@pytest.mark.asyncio
async def test_retention_cutoffs_and_object_failure_rollback(session, base_org) -> None:
    expired_token, recent_token, export = new_id(), new_id(), new_id()
    ref = ObjectRef.export(request_id=export)
    async with session.begin():
        await session.execute(
            text(
                "insert into password_reset_token(id,account_id,token_hash,expires_at,created_at) "
                "values(:old,:account,:old_hash,now()-interval '8 days',now()-interval '9 days'),"
                "(:recent,:account,:recent_hash,now()-interval '6 days',now()-interval '7 days')"
            ),
            {
                "old": expired_token,
                "recent": recent_token,
                "account": base_org["manager"],
                "old_hash": str(expired_token),
                "recent_hash": str(recent_token),
            },
        )
        await session.execute(
            text(
                "insert into export_request(id,org_id,subject_kind,subject_id,status,bundle_object_key,ready_at,expires_at) "
                "values(:id,:org,'account',:subject,'ready',:key,now()-interval '8 days',now()-interval '1 day')"
            ),
            {"id": export, "org": base_org["org"], "subject": base_org["manager"], "key": ref.key},
        )
        await sweep(session, retention_class="RC-3", object_store=MemoryObjects({}))
    async with session.begin():
        assert not await session.scalar(text("select exists(select 1 from password_reset_token where id=:id)"), {"id": expired_token})
        assert await session.scalar(text("select exists(select 1 from password_reset_token where id=:id)"), {"id": recent_token})
    with pytest.raises(RuntimeError, match="simulated object deletion failure"):
        async with session.begin():
            await sweep(
                session,
                retention_class="RC-8",
                object_store=FailingObjects({ref.key: b"bundle"}),
            )
    async with session.begin():
        assert await session.scalar(text("select bundle_object_key from export_request where id=:id"), {"id": export}) == ref.key


@pytest.mark.asyncio
async def test_restore_reopens_executed_request_if_personal_data_remains(
    session, base_org
) -> None:
    account, request, operator = new_id(), new_id(), new_id()
    marker = ErasureMarker(
        request_id=request,
        org_id=base_org["org"],
        subject_kind="account",
        subject_id=account,
        requested_at=datetime.now(UTC),
        armed_at=datetime.now(UTC),
        executed_by=operator,
    )
    async with session.begin():
        await session.execute(
            text(
                "insert into ops_account(id,email,display_name,password_hash,totp_secret_ciphertext) "
                "values(:id,:email,'Ops','x',decode('00','hex'))"
            ),
            {"id": operator, "email": f"p9-{operator}@t.test"},
        )
        await session.execute(
            text(
                "insert into account(id,org_id,team_id,email,display_name,role,password_hash,status) "
                "values(:id,:org,:id,:email,'Person','manager','x','deactivated')"
            ),
            {"id": account, "org": base_org["org"], "email": f"p9-{account}@t.test"},
        )
        await session.execute(
            text(
                "insert into erasure_request(id,org_id,subject_kind,subject_id,status,executed_by) "
                "values(:id,:org,'account',:subject,'executed',:actor)"
            ),
            {"id": request, "org": base_org["org"], "subject": account, "actor": operator},
        )
        assert await ensure_replay_pending(session, marker)
        assert await session.scalar(
            text("select status from erasure_request where id=:id"), {"id": request}
        ) == "pending"
        await execute_erasure(
            session,
            request_id=request,
            ledger=MemoryLedger(),
            object_store=MemoryObjects({}),
        )
    async with session.begin():
        assert not await ensure_replay_pending(session, marker)
        assert await session.scalar(
            text("select status from erasure_request where id=:id"), {"id": request}
        ) == "executed"


@pytest.mark.asyncio
async def test_stale_pending_recording_sweep_opens_one_fault(
    session, base_org, make_drill
) -> None:
    drill = await make_drill(status="published")
    old_attempt, recent_attempt = new_id(), new_id()
    async with session.begin():
        await session.execute(
            text(
                "insert into attempt(id,org_id,team_id,drill_id,rep_account_id,self_authored,"
                "status,started_at,ended_at,recording_status) values "
                "(:old,:org,:team,:drill,:rep,false,'grading_pending',"
                "now()-interval '20 minutes',now()-interval '11 minutes','pending'),"
                "(:recent,:org,:team,:drill,:rep,false,'grading_pending',"
                "now()-interval '5 minutes',now()-interval '1 minute','pending')"
            ),
            {
                "old": old_attempt,
                "recent": recent_attempt,
                "org": base_org["org"],
                "team": base_org["manager"],
                "drill": drill,
                "rep": base_org["manager"],
            },
        )
        assert await sweep_stale_recordings(session) == {
            "attempts_unavailable": 1,
            "faults_opened": 1,
        }
        assert await sweep_stale_recordings(session) == {
            "attempts_unavailable": 0,
            "faults_opened": 0,
        }
    async with session.begin():
        assert await session.scalar(
            text("select recording_status from attempt where id=:id"), {"id": old_attempt}
        ) == "unavailable"
        assert await session.scalar(
            text("select recording_status from attempt where id=:id"), {"id": recent_attempt}
        ) == "pending"
        assert await session.scalar(
            text("select count(*) from ops_fault where attempt_id=:id and kind='playback_asset'"),
            {"id": old_attempt},
        ) == 1
