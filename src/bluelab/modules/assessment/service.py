"""Candidate assessment readers and legal/preflight gate writer."""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.platform.errors import catalog
from bluelab.platform.errors.denial import ProblemError, ValidationProblem, not_found
from bluelab.platform.ids import new_id
from bluelab.platform.telemetry import metrics


async def assessment_view(session: AsyncSession, *, candidate_id: UUID) -> dict[str, Any]:
    candidate=(await session.execute(text("""select c.name,c.preflight,c.completed_at,p.title,app_candidate_org_name() org_name
      from candidate c join position p on p.id=c.position_id where c.id=:candidate"""), {"candidate":candidate_id})).mappings().one_or_none()
    if candidate is None: raise not_found()
    stages=(await session.execute(text("""select s.id,s.ord,d.call_type,d.label,
      count(a.id) filter(where a.status in ('completed','grading_pending','graded'))::int completed,
      count(a.id) filter(where a.status='interrupted')::int interrupted
      from assessment_stage s join drill d on d.id=s.drill_id left join attempt a on a.assessment_stage_id=s.id and a.candidate_id=:candidate
      group by s.id,d.id order by s.ord"""), {"candidate":candidate_id})).mappings().all()
    completed_count=sum(1 for row in stages if row['completed'])
    next_found=False; stage_data=[]
    for row in stages:
        if row['completed']: state='completed'
        elif row['interrupted'] >= 2: state='closed_incomplete'
        elif not next_found: state='next'; next_found=True
        else: state='upcoming'
        stage_data.append({'stage_id':row['id'],'ord':row['ord'],'status':state,'call_type':row['call_type'],
                           'label':None if state=='upcoming' else row['label'],'restart_available':row['interrupted']==1})
    preflight=candidate['preflight'] or {}; passed=bool(preflight.get('passed'))
    complete_at=candidate['completed_at']
    any_attempt=(await session.execute(text("select exists(select 1 from attempt where candidate_id=:candidate)"), {'candidate':candidate_id})).scalar_one()
    if complete_at: state='completed'
    elif not passed: state='preflight_required'
    elif any_attempt: state='in_progress'
    else: state='ready'
    duration=(await session.execute(text("select coalesce(sum(duration_seconds),0)::int from attempt where candidate_id=:candidate and status in ('completed','grading_pending','graded')"), {'candidate':candidate_id})).scalar_one()
    return {'candidate_name':candidate['name'],'position_title':candidate['title'],'org_name':candidate['org_name'],'language':'ar-EG','state':state,
            'gates':{'preflight_passed':passed,'consent_recorded':passed,'terms_accepted':passed},'progress':{'completed':completed_count,'total':len(stages)},'stages':stage_data,
            'completion': None if not complete_at else {'completed_at':complete_at,'stages_completed':completed_count,'total_seconds':duration}}

async def record_preflight(session: AsyncSession, *, candidate_id: UUID, checks: dict[str,bool], consent: bool, terms: bool) -> None:
    view=await assessment_view(session,candidate_id=candidate_id)
    if view['state']=='completed': raise ProblemError(catalog.ASSESSMENT_COMPLETED)
    errors=[]
    for key,value in checks.items():
        if not value: errors.append({'field':f'checks.{key}','message':'check must pass before the assessment can start'})
    if not consent: errors.append({'field':'consent','message':'recording consent is required'})
    if not terms: errors.append({'field':'terms_accepted','message':'Terms and Privacy acceptance is required'})
    if errors:
        metrics.record_candidate_probe(kind='preflight_blocked')
        raise ValidationProblem(errors)
    await session.execute(text("select app_record_candidate_preflight(:consent,:terms,cast(:preflight as jsonb))"),
      {'consent':new_id(),'terms':new_id(),'preflight':json.dumps({'passed':True,'microphone':True,'audio_output':True,'connection':True})})
    metrics.record_candidate_probe(kind='preflight_passed')

async def stage_brief(session: AsyncSession, *, candidate_id: UUID, stage_id: UUID) -> dict[str,Any]:
    view=await assessment_view(session,candidate_id=candidate_id)
    if view['state']=='completed': raise ProblemError(catalog.ASSESSMENT_COMPLETED)
    if view['state']=='preflight_required': raise ProblemError(catalog.PREFLIGHT_REQUIRED)
    stage=next((item for item in view['stages'] if item['stage_id']==stage_id),None)
    if stage is None: raise not_found()
    if stage['status']=='closed_incomplete': raise ProblemError(catalog.STAGE_CONSUMED)
    if stage['status']!='next': raise ProblemError(catalog.STAGE_NOT_NEXT,meta={'next_stage_ord':next((x['ord'] for x in view['stages'] if x['status']=='next'),None)})
    row=(await session.execute(text("""select d.id,d.label,d.call_type,d.lead_type,d.language,d.scenario,d.answer_key
      from assessment_stage s join drill d on d.id=s.drill_id where s.id=:stage and d.status='published'"""),{'stage':stage_id})).mappings().one_or_none()
    if row is None: raise ProblemError(catalog.DRILL_NOT_STARTABLE)
    scenario = row['scenario'] or {}
    snapshot = row['answer_key'] or {}
    facts = [
        str(fact['value'])
        for document in snapshot.get('documents', [])
        for fact in document.get('facts', [])
        if fact.get('value')
    ]
    # `persona` and the published answer-key snapshot are frozen drill content.
    # The candidate receives no concealed author inputs, rubric, score or review.
    return {'drill_id':row['id'],'label':row['label'],'call_type':row['call_type'],'lead_type':row['lead_type'],'language':row['language'],
            'buyer':scenario.get('persona', {}),'context':scenario.get('context',''),'product_summary':' '.join(facts[:3]),'stage':{'ord':stage['ord'],'of':view['progress']['total']}}


async def stage_reference(session: AsyncSession, *, candidate_id: UUID, stage_id: UUID) -> dict[str, Any]:
    """Read only the next stage's frozen reference; no cross-team live facts."""
    await stage_brief(session, candidate_id=candidate_id, stage_id=stage_id)
    row = (await session.execute(text("""select d.id,d.answer_key from assessment_stage s
      join drill d on d.id=s.drill_id where s.id=:stage and d.status='published'"""), {"stage": stage_id})).mappings().one_or_none()
    if row is None:
        raise not_found()
    snapshot = row["answer_key"] or {}
    documents = snapshot.get("documents", [])
    return {
        "drill_id": row["id"],
        "frozen": True,
        "groups": [
            {
                "document_title": document.get("title", "Product reference"),
                "facts": [
                    {"label": fact.get("label", "Fact"), "value": fact.get("value", ""), "note": fact.get("note")}
                    for fact in document.get("facts", [])
                ],
            }
            for document in documents
        ],
        "provenance": {
            "source_documents": [document.get("title", "Product reference") for document in documents],
            # Candidate RLS deliberately cannot read accounts; publisher identity
            # is not needed to use a reference and must not bypass that boundary.
            "published_by": "—",
            "fact_count": sum(len(document.get("facts", [])) for document in documents),
            "snapshot_at": snapshot.get("snapshot_at"),
        },
    }
