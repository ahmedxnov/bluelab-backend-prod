"""Drill authoring, freeze, brief and reference routes for Phase 3."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Body, Query, status

from bluelab.api.deps import (
    CurrentPrincipal,
    GenerationProviderDep,
    ManagerPrincipal,
    scope_of,
)
from bluelab.modules.drills import freeze, service
from bluelab.modules.drills.schemas import (
    AuthoringOptionList,
    BriefView,
    DrillFull,
    DrillInputs,
    GenerationAccepted,
    ReferenceView,
    RubricGenerationRequest,
    RubricView,
    WeightPatch,
)
from bluelab.platform.db.session import scoped_transaction

router = APIRouter(tags=["Drills"])


def _scope(record: CurrentPrincipal) -> tuple[UUID, UUID, UUID, bool]:
    return (
        UUID(record.org_id),
        UUID(record.team_id),
        UUID(record.account_id),
        record.role == "manager",
    )


@router.get(
    "/authoring-options",
    operation_id="listAuthoringOptions",
    response_model=AuthoringOptionList,
)
async def list_authoring_options(
    record: CurrentPrincipal,
    kind: Annotated[Literal["challenge", "hidden_motive"] | None, Query()] = None,
) -> AuthoringOptionList:
    async with scoped_transaction(scope_of(record)) as db:
        return AuthoringOptionList(data=await service.list_options(db, kind=kind))


@router.post(
    "/drills",
    operation_id="createDrill",
    response_model=DrillFull,
    status_code=status.HTTP_201_CREATED,
)
async def create_drill(
    payload: DrillInputs, record: CurrentPrincipal, provider: GenerationProviderDep
) -> DrillFull:
    org, team, viewer, manager = _scope(record)
    async with scoped_transaction(scope_of(record)) as db:
        drill_id = await service.create_drill(
            db,
            org_id=org,
            team_id=team,
            author_id=viewer,
            role=record.role,
            payload=payload,
            provider=provider,
        )
    async with scoped_transaction(scope_of(record)) as db:
        return await service.get_full(
            db,
            drill_id=drill_id,
            org_id=org,
            team_id=team,
            viewer_id=viewer,
            is_manager=manager,
        )


@router.get("/drills/{drill_id}", operation_id="getDrill", response_model=DrillFull)
async def get_drill(drill_id: UUID, record: CurrentPrincipal) -> DrillFull:
    org, team, viewer, manager = _scope(record)
    async with scoped_transaction(scope_of(record)) as db:
        return await service.get_full(
            db,
            drill_id=drill_id,
            org_id=org,
            team_id=team,
            viewer_id=viewer,
            is_manager=manager,
        )


@router.put(
    "/drills/{drill_id}/inputs",
    operation_id="replaceDrillInputs",
    response_model=DrillFull,
)
async def replace_drill_inputs(
    drill_id: UUID,
    payload: DrillInputs,
    record: CurrentPrincipal,
    provider: GenerationProviderDep,
) -> DrillFull:
    org, team, viewer, manager = _scope(record)
    async with scoped_transaction(scope_of(record)) as db:
        await service.replace_inputs(
            db,
            drill_id=drill_id,
            org_id=org,
            team_id=team,
            viewer_id=viewer,
            is_manager=manager,
            payload=payload,
            provider=provider,
        )
    async with scoped_transaction(scope_of(record)) as db:
        return await service.get_full(
            db,
            drill_id=drill_id,
            org_id=org,
            team_id=team,
            viewer_id=viewer,
            is_manager=manager,
        )


@router.post(
    "/drills/{drill_id}/scenario-generation",
    operation_id="generateScenario",
    response_model=GenerationAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def generate_scenario(
    drill_id: UUID, record: CurrentPrincipal
) -> GenerationAccepted:
    org, team, viewer, manager = _scope(record)
    async with scoped_transaction(scope_of(record)) as db:
        await service.request_scenario(
            db,
            drill_id=drill_id,
            org_id=org,
            team_id=team,
            viewer_id=viewer,
            is_manager=manager,
        )
    return GenerationAccepted()


@router.post(
    "/drills/{drill_id}/rubric-generation",
    operation_id="generateRubric",
    response_model=GenerationAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def generate_rubric(
    drill_id: UUID,
    record: CurrentPrincipal,
    payload: Annotated[RubricGenerationRequest | None, Body()] = None,
) -> GenerationAccepted:
    org, team, viewer, manager = _scope(record)
    async with scoped_transaction(scope_of(record)) as db:
        await service.request_rubric(
            db,
            drill_id=drill_id,
            org_id=org,
            team_id=team,
            viewer_id=viewer,
            is_manager=manager,
            discard_confirmed=payload.discard_confirmed if payload else None,
        )
    return GenerationAccepted()


@router.patch(
    "/drills/{drill_id}/rubric/weights",
    operation_id="tuneRubricWeights",
    response_model=RubricView,
)
async def tune_rubric_weights(
    drill_id: UUID, payload: WeightPatch, record: CurrentPrincipal
) -> RubricView:
    org, team, viewer, manager = _scope(record)
    async with scoped_transaction(scope_of(record)) as db:
        return await service.tune_weights(
            db,
            drill_id=drill_id,
            org_id=org,
            team_id=team,
            viewer_id=viewer,
            is_manager=manager,
            weights=[(item.dimension_id, item.weight) for item in payload.weights],
        )


@router.delete(
    "/drills/{drill_id}/rubric/dimensions/{dimension_id}",
    operation_id="deleteRubricDimension",
    response_model=RubricView,
)
async def delete_rubric_dimension(
    drill_id: UUID, dimension_id: UUID, record: CurrentPrincipal
) -> RubricView:
    org, team, viewer, manager = _scope(record)
    async with scoped_transaction(scope_of(record)) as db:
        return await service.delete_dimension(
            db,
            drill_id=drill_id,
            dimension_id=dimension_id,
            org_id=org,
            team_id=team,
            viewer_id=viewer,
            is_manager=manager,
        )


@router.post(
    "/drills/{drill_id}/publish", operation_id="publishDrill", response_model=DrillFull
)
async def publish_drill(drill_id: UUID, record: CurrentPrincipal) -> DrillFull:
    org, team, viewer, manager = _scope(record)
    async with scoped_transaction(scope_of(record)) as db:
        await freeze.publish_drill(
            db,
            drill_id=drill_id,
            org_id=org,
            team_id=team,
            viewer_id=viewer,
            is_manager=manager,
        )
    async with scoped_transaction(scope_of(record)) as db:
        return await service.get_full(
            db,
            drill_id=drill_id,
            org_id=org,
            team_id=team,
            viewer_id=viewer,
            is_manager=manager,
        )


@router.post(
    "/drills/{drill_id}/archive",
    operation_id="archiveDrill",
    response_model=DrillFull,
)
async def archive_drill(drill_id: UUID, record: ManagerPrincipal) -> DrillFull:
    org, team, viewer, _ = _scope(record)
    async with scoped_transaction(scope_of(record)) as db:
        await service.archive_drill(
            db,
            drill_id=drill_id,
            org_id=org,
            team_id=team,
            viewer_id=viewer,
        )
    async with scoped_transaction(scope_of(record)) as db:
        return await service.get_full(
            db,
            drill_id=drill_id,
            org_id=org,
            team_id=team,
            viewer_id=viewer,
            is_manager=True,
        )


@router.get(
    "/drills/{drill_id}/brief",
    operation_id="getDrillBrief",
    response_model=BriefView,
    response_model_exclude_none=True,
)
async def get_drill_brief(drill_id: UUID, record: CurrentPrincipal) -> BriefView:
    org, team, viewer, _ = _scope(record)
    async with scoped_transaction(scope_of(record)) as db:
        return await service.brief(
            db, drill_id=drill_id, org_id=org, team_id=team, viewer_id=viewer
        )


@router.get(
    "/drills/{drill_id}/reference",
    operation_id="getDrillReference",
    response_model=ReferenceView,
)
async def get_drill_reference(
    drill_id: UUID, record: CurrentPrincipal
) -> ReferenceView:
    org, team, viewer, _ = _scope(record)
    async with scoped_transaction(scope_of(record)) as db:
        return await service.reference(
            db, drill_id=drill_id, org_id=org, team_id=team, viewer_id=viewer
        )
