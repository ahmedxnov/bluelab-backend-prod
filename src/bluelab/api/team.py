"""The manager's coaching surface — `/team` (specs/21).

Paths, `operationId`s and status codes are `api/openapi.yaml`'s. The conformance
diff fails the build on any divergence (ADR-0035), so this file conforms to the
contract rather than describing it.

## Why this is a separate module from `training.py`

Not tidiness — the principal differs. Every route in `training.py` takes
`CurrentPrincipal`, because `/me` is about the caller and any signed-in account
may ask. Every route here takes `ManagerPrincipal`, because the answer is
legitimately about **other people**.

That distinction is worth a file boundary. A `/team` route added to the `/me`
router would inherit the wrong default by proximity, and the failure would be
silent: a rep would get a dashboard rather than a refusal, computed from their own
attempts alone and rendered as their team's. `tests/l1_unit/test_team_routes_are_gated.py`
catches that regardless of which file it happens in, but keeping the two surfaces
apart means it should never have to.

## Everything here operates on the manager's own team

FR-TRM-001. The scope tuple carries `team_id` from the session record, so no
header, path or query parameter can widen it, and the service passes it into
every query explicitly rather than leaning on RLS alone — defence in depth, since
here the policies would answer correctly anyway.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status

from bluelab.api.deps import ManagerPrincipal, scope_of
from bluelab.modules.training import service
from bluelab.modules.training.schemas import (
    CatalogStatus,
    Month,
    RepDeepDive,
    TeamCatalog,
    TeamDashboard,
    TeamRoster,
)
from bluelab.platform.db.session import scoped_transaction

router = APIRouter(tags=["Team"])

MonthQuery = Annotated[
    Month | None,
    Query(description="Calendar month YYYY-MM, interpreted in the org's timezone."),
]
"""Typed with the contract's pattern, so a malformed month is a `422` from
FastAPI before a query is built.

The pattern is the cheap first pass, not the guarantee: it admits `0000-01`,
which is not a calendar month. `service.parse_month` answers the rest, also with
a `422`, so the two agree on the status and a caller cannot tell which layer
refused them."""

CursorQuery = Annotated[
    str | None,
    Query(description="Opaque pagination cursor from a previous response."),
]
"""A plain string, matching the contract — the fuller reasoning is on
`api/training.py`'s copy. Typed as a `datetime` it would tell a generated client
the cursor is a timestamp it may construct, and the moment one does, the ordering
column is public API."""

LimitQuery = Annotated[int, Query(ge=1, le=100)]
"""Bounded in the signature, so FastAPI answers an out-of-range page size with
`422` before a query is built — the contract's answer and the cheapest one."""


@router.get(
    "/team/dashboard",
    operation_id="getTeamDashboard",
    response_model=TeamDashboard,
    status_code=status.HTTP_200_OK,
    summary="Team dashboard — the month's average, tiers, and where it hurts",
)
async def get_team_dashboard(
    record: ManagerPrincipal, month: MonthQuery = None
) -> TeamDashboard:
    """This month's coaching picture, in the org's calendar (FR-TRM-004).

    `month` chooses WHICH month, never WHOSE calendar — the timezone comes from
    the org and cannot be varied by a caller.
    """
    async with scoped_transaction(scope_of(record)) as db:
        return await service.team_dashboard(
            db,
            account_id=UUID(record.account_id),
            team_id=UUID(record.team_id),
            month=month,
        )


@router.get(
    "/team/roster",
    operation_id="getTeamRoster",
    response_model=TeamRoster,
    status_code=status.HTTP_200_OK,
    summary="Team roster — every rep, ranked, with their strongest and weakest",
)
async def get_team_roster(record: ManagerPrincipal, month: MonthQuery = None) -> TeamRoster:
    """Every active rep on the manager's team, rating descending (FR-TRM-005).

    Every rep, not every rated rep. Somebody who took no calls this month still
    has a row — they are the person most likely to need the conversation.
    """
    async with scoped_transaction(scope_of(record)) as db:
        return await service.team_roster(
            db,
            account_id=UUID(record.account_id),
            team_id=UUID(record.team_id),
            month=month,
        )


@router.get(
    "/team/drills",
    operation_id="getTeamDrillCatalog",
    response_model=TeamCatalog,
    status_code=status.HTTP_200_OK,
    summary="Team drill catalog — published, draft and archived, with their rollups",
)
async def get_team_drill_catalog(
    record: ManagerPrincipal,
    status_filter: Annotated[CatalogStatus, Query(alias="status")] = "all",
    cursor: CursorQuery = None,
    limit: LimitQuery = 25,
) -> TeamCatalog:
    """The team's drills, most recently updated first (FR-TRM-008).

    A rep's self-authored drills never appear, whatever the filter — FR-TRP-009
    keeps them private to their author, and this is the one surface that asks for
    *all* of a team's drills.

    `alias="status"` rather than a parameter simply named `status`: the query key
    the contract specifies is `status`, but that name shadows FastAPI's `status`
    module inside this function. Harmless today — the decorator resolved
    `status.HTTP_200_OK` before this signature existed — and a live trap for
    whoever next reaches for a status constant in here and silently gets a string.
    """
    async with scoped_transaction(scope_of(record)) as db:
        return await service.team_catalog(
            db,
            team_id=UUID(record.team_id),
            status=status_filter,
            cursor=cursor,
            limit=limit,
        )


@router.get(
    "/team/reps/{account_id}",
    operation_id="getRepDeepDive",
    response_model=RepDeepDive,
    status_code=status.HTTP_200_OK,
    summary="Rep deep dive — one rep's month, with the way into each replay",
)
async def get_rep_deep_dive(
    account_id: UUID, record: ManagerPrincipal, month: MonthQuery = None
) -> RepDeepDive:
    """One rep's month (FR-TRM-006).

    `account_id` is the first value on this surface that the caller chooses, so it
    is the first that can point at somebody they may not see. The service refuses
    with the generic `404` — never a `403`, which would confirm the rep exists and
    that this manager merely is not allowed (AC-IDA-006).
    """
    async with scoped_transaction(scope_of(record)) as db:
        return await service.rep_deep_dive(
            db,
            account_id=UUID(record.account_id),
            team_id=UUID(record.team_id),
            rep_account_id=account_id,
            month=month,
        )
