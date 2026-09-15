"""Attempt status and canonical viewer-projected Review routes."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status

from bluelab.api.deps import CurrentPrincipal, object_store_from_request, scope_of
from bluelab.modules.review import service
from bluelab.modules.review.schemas import AttemptView, ReviewView
from bluelab.platform.config import Settings, get_settings
from bluelab.platform.db.session import scoped_transaction

router = APIRouter(tags=["Attempts"])
SettingsDep = Annotated[Settings, Depends(get_settings)]


@router.get(
    "/attempts/{attempt_id}",
    operation_id="getAttempt",
    response_model=AttemptView,
    status_code=status.HTTP_200_OK,
    summary="Attempt record and status",
)
async def get_attempt(attempt_id: UUID, record: CurrentPrincipal) -> AttemptView:
    async with scoped_transaction(scope_of(record)) as db:
        return await service.attempt_view(
            db,
            attempt_id=attempt_id,
            org_id=UUID(record.org_id),
            team_id=UUID(record.team_id),
            account_id=UUID(record.account_id),
            is_manager=record.role == "manager",
        )


@router.get(
    "/attempts/{attempt_id}/review",
    operation_id="getReview",
    response_model=ReviewView,
    status_code=status.HTTP_200_OK,
    summary="The review",
)
async def get_review(
    attempt_id: UUID,
    record: CurrentPrincipal,
    request: Request,
    settings: SettingsDep,
) -> ReviewView:
    async with scoped_transaction(scope_of(record)) as db:
        return await service.review_view(
            db,
            attempt_id=attempt_id,
            org_id=UUID(record.org_id),
            team_id=UUID(record.team_id),
            account_id=UUID(record.account_id),
            is_manager=record.role == "manager",
            object_store_factory=lambda: object_store_from_request(request, settings),
            authorization_seconds=settings.object_presign_seconds,
        )
