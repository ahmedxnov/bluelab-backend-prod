"""Bearer-token candidate assessment routes."""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter

from bluelab.api.deps import CandidatePrincipal
from bluelab.modules.assessment import service
from bluelab.modules.assessment.schemas import (
    AssessmentView,
    CandidateReferenceView,
    PreflightSubmit,
    StageBriefView,
)
from bluelab.platform.db.scope import ScopeContext
from bluelab.platform.db.session import scoped_transaction

router=APIRouter(tags=['Assessment'])
def _scope(p: CandidatePrincipal) -> ScopeContext:
 return ScopeContext.candidate(org_id=p.org_id,team_id=p.team_id,position_id=p.position_id,candidate_id=p.candidate_id)
@router.get('/assessment',operation_id='getAssessment',response_model=AssessmentView)
async def get_assessment(principal: CandidatePrincipal) -> dict[str, object]:
 async with scoped_transaction(_scope(principal)) as db: return await service.assessment_view(db,candidate_id=principal.candidate_id)
@router.post('/assessment/preflight',operation_id='submitPreflight',response_model=AssessmentView)
async def submit_preflight(payload: PreflightSubmit, principal: CandidatePrincipal) -> dict[str, object]:
 async with scoped_transaction(_scope(principal)) as db:
  await service.record_preflight(db,candidate_id=principal.candidate_id,checks=payload.checks.model_dump(),consent=payload.consent,terms=payload.terms_accepted)
  return await service.assessment_view(db,candidate_id=principal.candidate_id)
@router.get('/assessment/stages/{stage_id}/brief',operation_id='getStageBrief',response_model=StageBriefView)
async def get_stage_brief(stage_id: UUID, principal: CandidatePrincipal) -> dict[str, object]:
 async with scoped_transaction(_scope(principal)) as db: return await service.stage_brief(db,candidate_id=principal.candidate_id,stage_id=stage_id)

@router.get('/assessment/stages/{stage_id}/reference', operation_id='getStageReference', response_model=CandidateReferenceView)
async def get_stage_reference(stage_id: UUID, principal: CandidatePrincipal) -> dict[str, object]:
 async with scoped_transaction(_scope(principal)) as db: return await service.stage_reference(db,candidate_id=principal.candidate_id,stage_id=stage_id)
