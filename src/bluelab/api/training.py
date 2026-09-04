"""The rep surface — progress, profile, and the coaching feed.

Paths, `operationId`s and status codes are `api/openapi.yaml`'s. The conformance
diff fails the build on any divergence (ADR-0035), so this file conforms to the
contract rather than describing it.

## Why these routes are here and not in `modules/training/router.py`

The same reason Auth's are. Every route needs `bluelab.api.deps` for the resolved
principal, and `bluelab.modules` sits BELOW `bluelab.api` in the layers contract —
so a router inside the module importing `deps` is an upward import and a build
failure.

`modules/identity/router.py` frames Auth as "the exception". It is not: it is the
pattern. Any route that needs a principal — which is every route on the customer
surface — lands in `bluelab.api`, and the module keeps the behaviour. The rule is
about which layer may know about request handling, and it does not bend for the
second module to need it.

The training *behaviour* stays in `modules.training.service`: this file resolves a
principal, opens a scoped transaction, and renders. It holds no decisions.

## Everything here takes `CurrentPrincipal`

Not `GatedPrincipal`. This is product surface, so a session still behind
first-sign-in, consent or terms is refused with the `409` naming its gate — the
deny-by-default direction `api.deps` is built around. Only the two gate exits
tolerate a limited session.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status

from bluelab.api.deps import CurrentPrincipal, scope_of
from bluelab.modules.training import service
from bluelab.modules.training.schemas import (
    AttemptedFilter,
    CallType,
    CoachFeedbackPage,
    DrillHistory,
    LibraryPage,
    LibrarySort,
    MarkReadRequest,
    MarkReadResult,
    ProfileView,
    ProgressHome,
    SourceFilter,
)
from bluelab.platform.db.session import scoped_transaction

router = APIRouter(tags=["Me"])

CursorQuery = Annotated[
    str | None,
    Query(description="Opaque pagination cursor from a previous response."),
]
"""A plain string, matching the contract.

Typed as `datetime` this served `format: date-time`, which is drift twice over: it
disagrees with the contract's `type: string`, and it tells a generated client the
cursor is a timestamp it may construct. The moment a client builds its own, the
ordering column is part of the public API. `service.encode_cursor` keeps it
opaque; this keeps the declared type honest about that.
"""
LimitQuery = Annotated[int, Query(ge=1, le=100)]
"""Bounded in the signature, not in the service.

An unbounded `limit` is a client-supplied denial of service against ourselves,
and FastAPI rejects an out-of-range one with `422` before a query is built —
which is both the contract's answer and the cheapest possible one.
"""


@router.get(
    "/me/progress",
    operation_id="getMyProgress",
    response_model=ProgressHome,
    status_code=status.HTTP_200_OK,
    summary="Rep home — monthly rating, trend, and where to work next",
)
async def get_my_progress(
    record: CurrentPrincipal,
    call_type: Annotated[CallType | None, Query()] = None,
) -> ProgressHome:
    """This month's standing, in the org's timezone.

    `call_type` narrows the weekly trend only; the headline rating stays
    all-types, so switching the filter cannot appear to change where the rep
    actually stands.
    """
    async with scoped_transaction(scope_of(record)) as db:
        return await service.progress_home(
            db, account_id=UUID(record.account_id), call_type=call_type
        )


@router.get(
    "/me/profile",
    operation_id="getMyProfile",
    response_model=ProfileView,
    status_code=status.HTTP_200_OK,
    summary="The rep's own profile and badges",
)
async def get_my_profile(record: CurrentPrincipal) -> ProfileView:
    """Owner-only (FR-TRP-005).

    There is no team-facing counterpart and no manager policy on `badge_award`, so
    a leaderboard cannot accidentally join to it. The privacy is a property of the
    schema; this route simply does not offer another id to ask about.
    """
    async with scoped_transaction(scope_of(record)) as db:
        return await service.profile(db, account_id=UUID(record.account_id))


@router.get(
    "/me/coach-feedback",
    operation_id="listCoachFeedback",
    response_model=CoachFeedbackPage,
    status_code=status.HTTP_200_OK,
    summary="The coaching feed, newest first",
)
async def list_coach_feedback(
    record: CurrentPrincipal,
    cursor: CursorQuery = None,
    limit: LimitQuery = 25,
) -> CoachFeedbackPage:
    """Keyset-paginated on `created_at`.

    The feed grows at the head, so an offset page would shift under the reader
    between requests and silently repeat or skip an item.
    """
    async with scoped_transaction(scope_of(record)) as db:
        return await service.coach_feedback(
            db, account_id=UUID(record.account_id), cursor=cursor, limit=limit
        )


@router.get(
    "/me/library",
    operation_id="getMyLibrary",
    response_model=LibraryPage,
    status_code=status.HTTP_200_OK,
    summary="My drill library",
)
async def get_my_library(
    record: CurrentPrincipal,
    source: Annotated[SourceFilter, Query()] = "all",
    call_type: Annotated[CallType | None, Query()] = None,
    attempted: Annotated[AttemptedFilter, Query()] = "all",
    sort: Annotated[LibrarySort, Query()] = "recommended",
    cursor: CursorQuery = None,
    limit: LimitQuery = 25,
) -> LibraryPage:
    """One grid of everything the rep can practice (FR-TRP-006).

    The four filters are `Literal` types rather than plain strings, so an unknown
    value is a `422` from FastAPI before a query is built. That matters more here
    than it looks: `source` and `attempted` reach SQL as bind parameters compared
    against literals, and a value outside the enum would silently match no branch
    and return an empty grid — a filter that appears to work and quietly hides
    everything.
    """
    async with scoped_transaction(scope_of(record)) as db:
        return await service.library(
            db,
            account_id=UUID(record.account_id),
            team_id=UUID(record.team_id),
            source=source,
            call_type=call_type,
            attempted=attempted,
            sort=sort,
            cursor=cursor,
            limit=limit,
        )


@router.get(
    "/drills/{drill_id}/my-history",
    operation_id="getMyDrillHistory",
    response_model=DrillHistory,
    status_code=status.HTTP_200_OK,
    summary="My history on a drill",
    tags=["Library"],
)
async def get_my_drill_history(drill_id: UUID, record: CurrentPrincipal) -> DrillHistory:
    """The caller's own statistics and attempts on one drill (FR-TRP-011).

    Not under `/me`, and the contract puts it here deliberately: the subject is
    the drill, and `my-` is what scopes it to the caller. The scoping is still
    server-side — `account_id` comes off the session record, and there is no
    parameter that could name anyone else.

    `drill_id` is typed `UUID`, so a malformed one is a `422` from FastAPI before
    a query runs. An unknown or invisible one is a `404` from the service, and
    those two are the same answer on purpose (ADR-0036).
    """
    async with scoped_transaction(scope_of(record)) as db:
        return await service.drill_history(
            db, account_id=UUID(record.account_id), drill_id=drill_id
        )


@router.post(
    "/me/coach-feedback/mark-read",
    operation_id="markCoachFeedbackRead",
    response_model=MarkReadResult,
    status_code=status.HTTP_200_OK,
    summary="Mark the feed read through an item",
)
async def mark_coach_feedback_read(
    payload: MarkReadRequest, record: CurrentPrincipal
) -> MarkReadResult:
    """Marks the named item and every older one read, and returns the new count.

    Returning the count means the client updates its badge from this response
    instead of re-fetching the page — the unread badge is the only notification
    surface in v1, so a stale one is conspicuous.
    """
    async with scoped_transaction(scope_of(record)) as db:
        return await service.mark_feedback_read(
            db, account_id=UUID(record.account_id), through_id=payload.through_id
        )
