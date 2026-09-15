"""Phase 3 content lifecycle invariants against real PostgreSQL."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from bluelab.adapters.generation_llm import GeneratedScenario
from bluelab.modules.drills import service as drill_service
from bluelab.modules.knowledge import service as knowledge_service
from bluelab.platform.ids import new_id

pytestmark = [
    pytest.mark.l3_integration,
    pytest.mark.l7_security,
    pytest.mark.invariant_path,
]


@pytest.mark.verifies("FR-KNW-003", "FR-KNW-008")
async def test_extraction_failure_preserves_live_facts(session, base_org):
    document_id, live_id, fact_id, upload_id = (new_id() for _ in range(4))
    async with session.begin():
        await session.execute(
            text(
                "insert into product_document(id,org_id,team_id,title,live_version) "
                "values(:document,:org,:team,'Policy',1)"
            ),
            {
                "document": document_id,
                "org": base_org["org"],
                "team": base_org["manager"],
            },
        )
        await session.execute(
            text(
                "insert into fact_set(id,org_id,team_id,document_id,kind,source,created_by) "
                "values(:live,:org,:team,:document,'live','manual',:author)"
            ),
            {
                "live": live_id,
                "org": base_org["org"],
                "team": base_org["manager"],
                "document": document_id,
                "author": base_org["manager"],
            },
        )
        await session.execute(
            text(
                "insert into product_fact(id,org_id,team_id,fact_set_id,ord,label,value) "
                "values(:fact,:org,:team,:live,1,'Limit','EGP 1m')"
            ),
            {
                "fact": fact_id,
                "org": base_org["org"],
                "team": base_org["manager"],
                "live": live_id,
            },
        )
        await session.execute(
            text(
                "insert into document_upload(id,org_id,team_id,document_id,object_key,"
                "filename,byte_size,created_by,status) values(:upload,:org,:team,:document,"
                ":key,'bad.pdf',10,:author,'extracting')"
            ),
            {
                "upload": upload_id,
                "org": base_org["org"],
                "team": base_org["manager"],
                "document": document_id,
                "key": f"orgs/{base_org['org']}/uploads/{upload_id}",
                "author": base_org["manager"],
            },
        )
        await knowledge_service.mark_extraction_failed(session, upload_id=upload_id)

    status, value, live_version = (
        await session.execute(
            text(
                "select u.status,f.value,d.live_version from document_upload u "
                "join product_document d on d.id=u.document_id "
                "join fact_set s on s.document_id=d.id and s.kind='live' "
                "join product_fact f on f.fact_set_id=s.id where u.id=:upload"
            ),
            {"upload": upload_id},
        )
    ).one()
    assert (status, value, live_version) == ("failed", "EGP 1m", 1)


@pytest.mark.verifies("FR-DRL-005", "FR-DRL-008")
async def test_stale_scenario_result_cannot_overwrite_newer_request(
    session, make_drill
):
    drill_id = await make_drill(status="draft")
    stale_request, current_request = new_id(), new_id()
    async with session.begin():
        await session.execute(
            text(
                "update drill set scenario_generation_status='running',"
                "scenario_generation_request_id=:request where id=:drill"
            ),
            {"request": current_request, "drill": drill_id},
        )
        applied = await drill_service.apply_scenario(
            session,
            drill_id=drill_id,
            request_id=stale_request,
            result=GeneratedScenario.model_validate(
                {
                    "label": "Mona — Buyer",
                    "persona": {
                        "name": "Mona",
                        "role": "Buyer",
                        "company": "Acme",
                        "meta_facts": [],
                    },
                    "context": "Context",
                    "product_references": [],
                }
            ),
        )
    scenario, request_id, status = (
        await session.execute(
            text(
                "select scenario,scenario_generation_request_id,scenario_generation_status "
                "from drill where id=:drill"
            ),
            {"drill": drill_id},
        )
    ).one()
    assert applied is False
    assert scenario is None
    assert request_id == current_request
    assert status == "running"


@pytest.mark.verifies("FR-DRL-006")
async def test_database_rejects_two_running_generation_kinds(session, make_drill):
    drill_id = await make_drill(status="draft")
    with pytest.raises(DBAPIError, match="single_generation_lane"):
        async with session.begin():
            await session.execute(
                text(
                    "update drill set scenario_generation_status='running',"
                    "rubric_generation_status='running' where id=:drill"
                ),
                {"drill": drill_id},
            )
    await session.rollback()


@pytest.mark.verifies("FR-DRL-014", "SEC-030")
async def test_participant_brief_projection_omits_concealed_fields(
    session, base_org, make_drill
):
    drill_id = await make_drill(status="draft")
    participant_id = new_id()
    async with session.begin():
        await session.execute(
            text(
                "update drill set status='published',published_at=now(),label='Mona — Buyer',"
                "scenario=cast(:scenario as jsonb),answer_key=cast(:answer_key as jsonb) "
                "where id=:drill"
            ),
            {
                "drill": drill_id,
                "scenario": (
                    '{"v":1,"persona":{"name":"Mona","role":"Buyer",'
                    '"company":"Acme","meta_facts":[]},"context":"Context",'
                    '"product_references":[]}'
                ),
                "answer_key": '{"v":1,"documents":[]}',
            },
        )
        await session.execute(
            text(
                "insert into account(id,org_id,team_id,email,display_name,role,password_hash) "
                "values(:id,:org,:team,:email,'Rep','rep','x')"
            ),
            {
                "id": participant_id,
                "org": base_org["org"],
                "team": base_org["manager"],
                "email": f"phase3-{participant_id}@test.example",
            },
        )
    brief = await drill_service.brief(
        session,
        drill_id=drill_id,
        org_id=base_org["org"],
        team_id=base_org["manager"],
        viewer_id=participant_id,
    )
    payload = brief.model_dump(exclude_none=True)
    assert "author_recap" not in payload
    assert "challenges" not in payload
    assert "hidden_motives" not in payload
