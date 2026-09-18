"""Phase 8 T-1/T-6 candidate and compensation proofs on real PostgreSQL."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from bluelab.calls.admission import AdmissionRequest, ParticipantKind, admit
from bluelab.calls.interruption import Disposition, interrupt
from bluelab.platform.db.privileged import system_scope
from bluelab.platform.db.scope import apply_scope
from bluelab.platform.errors.denial import ProblemError
from bluelab.platform.ids import new_id

pytestmark = [pytest.mark.l3_integration, pytest.mark.invariant_path]


async def _candidate_plan(session, base_org, make_drill):
    first, second = await make_drill(), await make_drill()
    position, candidate, first_stage, second_stage = new_id(), new_id(), new_id(), new_id()
    async with session.begin():
        await session.execute(text("""insert into position(id,org_id,team_id,title,openings,status)
          values(:id,:org,:team,'AE',1,'active')"""), {"id":position,"org":base_org["org"],"team":base_org["manager"]})
        await session.execute(text("""insert into assessment_stage(id,org_id,team_id,position_id,ord,drill_id)
          values(:s1,:org,:team,:position,1,:d1),(:s2,:org,:team,:position,2,:d2)"""),
          {"s1":first_stage,"s2":second_stage,"org":base_org["org"],"team":base_org["manager"],"position":position,"d1":first,"d2":second})
        await session.execute(text("""insert into candidate(id,org_id,team_id,position_id,name,email,preflight)
          values(:id,:org,:team,:position,'Candidate',:email,cast(:preflight as jsonb))"""),
          {"id":candidate,"org":base_org["org"],"team":base_org["manager"],"position":position,"email":f"p8-{candidate}@t.test","preflight":'{"passed":true}'})
        await session.execute(text("""insert into consent_record(id,org_id,candidate_id,notice_version)
          values(:id,:org,:candidate,'v1')"""), {"id":new_id(),"org":base_org["org"],"candidate":candidate})
    return candidate, ((first_stage, first), (second_stage, second))


@pytest.mark.verifies("FR-CND-004", "FR-CND-010", "FR-CND-011")
async def test_second_interruption_consumes_stage_and_progresses_idempotently(session, base_org, make_drill):
    candidate, stages = await _candidate_plan(session, base_org, make_drill)
    for stage_id, drill_id in stages:
        request = AdmissionRequest(kind=ParticipantKind.CANDIDATE, org_id=base_org["org"], team_id=base_org["manager"], drill_id=drill_id, candidate_id=candidate, assessment_stage_id=stage_id)
        async with session.begin():
            await apply_scope(session, system_scope(org_id=base_org["org"]))
            first = await admit(session, request, consent_version="v1")
            assert first.restart is False
        async with session.begin():
            assert await interrupt(session, attempt_id=first.attempt_id, drill_id=drill_id, rep_account_id=None, disposition=Disposition.GRACE_EXCEEDED, candidate_id=candidate, org_id=base_org["org"], team_id=base_org["manager"])
        async with session.begin():
            await apply_scope(session, system_scope(org_id=base_org["org"]))
            restart = await admit(session, request, consent_version="v1")
            assert restart.restart is True
        async with session.begin():
            assert await interrupt(session, attempt_id=restart.attempt_id, drill_id=drill_id, rep_account_id=None, disposition=Disposition.GRACE_EXCEEDED, candidate_id=candidate, org_id=base_org["org"], team_id=base_org["manager"])
        async with session.begin():
            assert not await interrupt(session, attempt_id=restart.attempt_id, drill_id=drill_id, rep_account_id=None, disposition=Disposition.GRACE_EXCEEDED, candidate_id=candidate, org_id=base_org["org"], team_id=base_org["manager"])

    completed_at = (await session.execute(text("select completed_at from candidate where id=:id"), {"id":candidate})).scalar_one()
    jobs = (await session.execute(text("select count(*) from procrastinate_jobs where task_name='render_report' and args->'args'->>'candidate_id'=:id"), {"id":str(candidate)})).scalar_one()
    assert completed_at is not None
    assert jobs == 1
    await session.rollback()  # end the read-only autobegin before the refusal proof

    first_stage, first_drill = stages[0]
    with pytest.raises(ProblemError) as caught:
        async with session.begin():
            await apply_scope(session, system_scope(org_id=base_org["org"]))
            await admit(session, AdmissionRequest(kind=ParticipantKind.CANDIDATE, org_id=base_org["org"], team_id=base_org["manager"], drill_id=first_drill, candidate_id=candidate, assessment_stage_id=first_stage), consent_version="v1")
    assert caught.value.problem.slug == "stage-consumed"


@pytest.mark.verifies("FR-LIV-016", "FR-TRP-013")
async def test_never_established_deletes_attempt_and_reverses_allowance(session, base_org, make_drill):
    drill, rep, assignment = await make_drill(), new_id(), new_id()
    async with session.begin():
        await session.execute(text("""insert into account(id,org_id,team_id,email,display_name,role,password_hash)
          values(:id,:org,:team,:email,'Rep','rep','x')"""), {"id":rep,"org":base_org["org"],"team":base_org["manager"],"email":f"p8-{rep}@t.test"})
        await session.execute(text("insert into consent_record(id,org_id,account_id,notice_version) values(:id,:org,:rep,'v1')"), {"id":new_id(),"org":base_org["org"],"rep":rep})
        await session.execute(text("""insert into assignment(id,org_id,team_id,drill_id,due_date,attempts_allowed,created_by)
          values(:id,:org,:team,:drill,current_date,1,:team)"""), {"id":assignment,"org":base_org["org"],"team":base_org["manager"],"drill":drill})
        await session.execute(text("insert into assignment_recipient(assignment_id,org_id,team_id,rep_account_id) values(:id,:org,:team,:rep)"), {"id":assignment,"org":base_org["org"],"team":base_org["manager"],"rep":rep})
    async with session.begin():
        await apply_scope(session, system_scope(org_id=base_org["org"]))
        admitted = await admit(session, AdmissionRequest(kind=ParticipantKind.REP, org_id=base_org["org"], team_id=base_org["manager"], drill_id=drill, account_id=rep), consent_version="v1")
    async with session.begin():
        assert await interrupt(session, attempt_id=admitted.attempt_id, drill_id=drill, rep_account_id=rep, disposition=Disposition.NEVER_ESTABLISHED)
    assert (await session.execute(text("select count(*) from attempt where id=:id"), {"id":admitted.attempt_id})).scalar_one() == 0
    assert (await session.execute(text("select attempts_used from assignment_recipient where assignment_id=:id"), {"id":assignment})).scalar_one() == 0
