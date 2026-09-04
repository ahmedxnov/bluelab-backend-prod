"""Domain services — this module's behaviour, and its published interface to the
other modules. Cross-module callers enter here; they never touch `models.py`.

## Why this module reads through views and raw SQL

Everything on the rep surface is an aggregate over `attempt`, `scorecard` and
`drill` — tables owned by `review` and `drills`. The ADR-0012 module contract
forbids reaching into another module's stored state, and `import-linter` makes
that a build failure, so this module cannot import their models.

The V-1…V-11 views are the sanctioned seam (ADR-0034). They are
`security_invoker`, so a read through them runs under the CALLER's scope and the
same RLS policies apply — a rep sees its own rows and a manager its team's,
exactly as on the base tables. Naming a view is not a boundary violation: no
Python symbol crosses, and the view is a published contract in the same sense
`service.py` is.

## The banding rule is called, never re-implemented

`fn_score_band` is the single definition of green/amber/red (FR-SCR-005), so
every band in every response comes back from the database that computed the
score. A Python copy would be a second rule that agrees right up until someone
edits one of them.

## RLS is the boundary, but on `/me` it is NOT sufficient on its own

Verified: a REP querying `v_counted_attempt` with no account predicate at all
sees exactly one rep — itself. So the isolation is real and the explicit
`rep_account_id = :account` in every query below is defence in depth.

A MANAGER is different, and this is the trap. Through the same view a manager
legitimately sees their whole team — that is what the team surface is built on.
So on `/me`, where the answer must be about the caller alone, **the explicit
filter is load-bearing**: a `/me` query that omitted it would aggregate a
manager's entire team into their personal rating and report it as their own.

Every query here filters by the account for that reason, not out of habit.

## Ratings come from the COUNTED pool

`v_counted_attempt` excludes self-authored practice, which is the whole point of
it: a rep's private rehearsal must not move the number their manager sees, and
AC-TRP-004 keeps that practice concealed. Anything that reads like a rating —
monthly, lifetime, per-call-type, weekly — reads that pool.
"""

from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
from binascii import Error as BinasciiError
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.modules.training.schemas import (
    TIERING_MINIMUM,
    AttemptedFilter,
    AttemptSummary,
    BadgeView,
    CallType,
    CallTypeCard,
    CallTypeProfile,
    CatalogItem,
    CatalogStatus,
    CoachFeedbackItemView,
    CoachFeedbackPage,
    CursorPage,
    Direction,
    DrillHistory,
    DrillStats,
    GapRow,
    LibraryAssignment,
    LibraryCard,
    LibraryCounts,
    LibraryPage,
    LibrarySort,
    LibrarySource,
    MarkReadResult,
    ProfileView,
    ProgressHome,
    RecentAttempt,
    RepDeepDive,
    RosterRow,
    ScoreBand,
    Severity,
    SourceFilter,
    TeamCatalog,
    TeamDashboard,
    TeamRoster,
    WeeklyPoint,
)
from bluelab.platform.errors import catalog
from bluelab.platform.errors.denial import ProblemError, ValidationProblem, not_found

CALL_TYPES: tuple[CallType, ...] = ("discovery", "post_proposal", "renewal", "upsell")
"""All four, always returned. A card set that omitted the untried ones would hide
exactly the call type a rep has been avoiding (FR-TRP-001)."""

FLAT_BAND = 0.1
"""Below this, in absolute terms, the month reads `flat`.

Ratings carry one decimal, so a delta of 0.05 is rounding noise. Without a dead
band it would render as a direction arrow and be read as a trend.
"""


def _score_band(score: object, band: object) -> ScoreBand | None:
    """Pair a score with the band the database computed for it."""
    if score is None or band is None:
        return None
    return ScoreBand(score=float(score), band=band)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class MonthAnchor:
    """The reader's timezone and the month that is 'now' for them.

    No `org_id`: the scope tuple already carries it and every query here is
    filtered by the account, so a second copy would be a field that could
    disagree with the GUC the RLS policies actually read.
    """

    timezone: str
    month: datetime


_ANCHOR = text(
    """
    select o.timezone,
           date_trunc('month', pg_catalog.now() at time zone o.timezone) as month
    from account a join org o on o.id = a.org_id
    where a.id = :account
    """
)
"""The reader's own row plus its org's clock.

Every month boundary on this surface is the ORG's, not the server's. A rep in
Cairo and a process in Frankfurt must agree on which month an attempt fell in, or
the last day of every month reports two different ratings depending on who asks.
`v_counted_attempt` buckets the same way; this is how the API asks for the same
bucket.
"""


async def _anchor(session: AsyncSession, account_id: UUID) -> MonthAnchor:
    """Resolve the reader's org clock.

    Raises:
        ProblemError: `401 session-invalid` if the account is gone — reached only
            when a session outlives its account, and it does not distinguish that
            from any other invalid session.
    """
    row = (await session.execute(_ANCHOR, {"account": account_id})).one_or_none()
    if row is None:
        raise ProblemError(catalog.SESSION_INVALID)
    return MonthAnchor(timezone=row.timezone, month=row.month)


def parse_month(value: str) -> datetime:
    """`YYYY-MM` → the naive local timestamp `v_counted_attempt.local_month` holds.

    V-1 computes that column as `date_trunc('month', started_at at time zone
    o.timezone)`. Applying `at time zone` to a `timestamptz` yields a **naive**
    `timestamp` in the org's civil calendar, so the value compared against it must
    be naive too — an aware one would be coerced through the session `TimeZone`
    and land a month out at every boundary for any org not at UTC.

    ## The pattern is not sufficient, which is why this validates

    `schemas.MONTH_PATTERN` admits `0000-01` — four digits is four digits — and
    Python's `MINYEAR` is 1, so `strptime` rejects it. A caller who had done
    everything the contract asks could still land a `ValueError` here, and an
    uncaught one renders as `500 internal-error`: a server fault in the logs and
    on the dashboards, for what is simply bad input.

    So the guarantee lives here rather than in a regex every future caller has to
    remember to apply. The pattern stays as the cheap first pass at the boundary;
    this is the one that actually holds.

    Args:
        value: A month in `YYYY-MM`.

    Returns:
        Midnight on the first of that month, naive.

    Raises:
        ValidationProblem: `422 validation-error` with `errors[]` naming `month`,
            for anything `strptime` refuses. The message is fixed and does not
            echo the input.
    """
    try:
        return datetime.strptime(value, "%Y-%m")  # noqa: DTZ007 — naive by contract, see above
    except ValueError as exc:
        raise ValidationProblem(
            [{"field": "month", "message": "Not a calendar month in YYYY-MM form."}]
        ) from exc


async def resolve_anchor(
    session: AsyncSession, *, account_id: UUID, month: str | None
) -> MonthAnchor:
    """The org's clock, with the caller's month substituted if they named one.

    The timezone is never substituted, only the month. Every query built from an
    anchor still buckets on `anchor.timezone`, so `?month=` chooses WHICH month
    and never WHOSE calendar — a manager cannot ask to see their team's numbers
    in some other org's civil month by varying a parameter.

    Args:
        session: A scoped session; the underlying read is RLS-filtered.
        account_id: The reader, whose org supplies the timezone.
        month: `YYYY-MM`, or `None` for the org's current month.

    Raises:
        ProblemError: `401 session-invalid` if the account is gone.
    """
    anchor = await _anchor(session, account_id)
    if month is None:
        return anchor
    return replace(anchor, month=parse_month(month))


# ── GET /me/progress ──────────────────────────────────────────────────────────

_MONTH_RATING = text(
    """
    select t.rating, fn_score_band(t.rating) as band, t.trend
    from v_rep_month_trend t
    where t.rep_account_id = :account and t.month = :month
    """
)

_WEEKLY = text(
    """
    select date_trunc('week', v.started_at at time zone :tz)::date as week_start,
           round(avg(v.overall_score), 1)                          as score
    from v_counted_attempt v
    where v.rep_account_id = :account
      and v.local_month = :month
      and (cast(:call_type as text) is null or v.call_type = :call_type)
    group by 1
    order by 1
    """
)

_CALL_TYPE_CARDS = text(
    """
    select v.call_type,
           round(avg(v.overall_score), 1)                as rating,
           fn_score_band(round(avg(v.overall_score), 1)) as band,
           count(*)                                      as attempts
    from v_counted_attempt v
    where v.rep_account_id = :account
      and v.started_at >= pg_catalog.now() - interval '30 days'
    group by 1
    """
)
"""Trailing 30 days, NOT the calendar month.

FR-TRP-001 asks "what should I work on now", and on the second of the month a
calendar window answers it with one day of data. The month card above is the
scorecard; this is the prompt.
"""


def _direction(delta: float | None) -> Direction | None:
    """A month with no prior month has no direction — null, not `flat`.

    `flat` is a finding. Null is an absence, and a first-month rep has not been
    flat; they have not been measured twice.
    """
    if delta is None:
        return None
    if abs(delta) < FLAT_BAND:
        return "flat"
    return "rising" if delta > 0 else "slipping"


def _cards(rows: list[Any]) -> list[CallTypeCard]:
    """One card per call type, weakest first, exactly one tagged.

    Call types with no attempts in the window are included with a null rating and
    a zero count, and they sort LAST rather than weakest: an untried call type is
    unmeasured, not bad, and tagging it would send the rep to work on the thing
    the product knows least about.
    """
    measured = {row.call_type: row for row in rows}
    cards = [
        CallTypeCard(
            call_type=kind,
            rating=_score_band(measured[kind].rating, measured[kind].band)
            if kind in measured
            else None,
            attempts=int(measured[kind].attempts) if kind in measured else 0,
            weakest=False,
        )
        for kind in CALL_TYPES
    ]
    cards.sort(key=lambda c: (c.rating is None, c.rating.score if c.rating else 0.0))
    if cards and cards[0].rating is not None:
        cards[0] = cards[0].model_copy(update={"weakest": True})
    return cards


async def progress_home(
    session: AsyncSession, *, account_id: UUID, call_type: CallType | None
) -> ProgressHome:
    """The rep home: this month's rating, its trend, and where to work next.

    Args:
        session: A transaction scoped to the reading rep.
        account_id: From the session record, never from the request.
        call_type: Narrows the WEEKLY TREND only. The headline rating stays
            all-types, so changing the filter cannot appear to change the rep's
            actual standing.

    Returns:
        `ProgressHome`, every score already banded by the database.
    """
    anchor = await _anchor(session, account_id)
    params = {"account": account_id, "month": anchor.month}

    summary = (await session.execute(_MONTH_RATING, params)).one_or_none()
    weekly = (
        await session.execute(
            _WEEKLY, {**params, "tz": anchor.timezone, "call_type": call_type}
        )
    ).all()
    cards = (await session.execute(_CALL_TYPE_CARDS, {"account": account_id})).all()

    delta = float(summary.trend) if summary is not None and summary.trend is not None else None
    return ProgressHome(
        month=anchor.month.strftime("%Y-%m"),
        rating=_score_band(summary.rating, summary.band) if summary is not None else None,
        delta=delta,
        direction=_direction(delta),
        weekly_trend=[
            WeeklyPoint(
                week_start=row.week_start,
                score=float(row.score) if row.score is not None else None,
            )
            for row in weekly
        ],
        call_type_cards=_cards(list(cards)),
    )


# ── GET /me/profile ───────────────────────────────────────────────────────────

_PROFILE = text(
    """
    with lifetime as (
        select (
            select count(*) from attempt a
            join scorecard s on s.attempt_id = a.id
            where a.rep_account_id = :account and a.status = 'graded'
        ) as graded,
        (
            select round(avg(v.overall_score), 1) from v_counted_attempt v
            where v.rep_account_id = :account
        ) as rating
    )
    select a.display_name, a.email,
           l.graded, l.rating, fn_score_band(l.rating) as band
    from account a cross join lifetime l
    where a.id = :account
    """
)
"""The two figures come from DIFFERENT pools, and that is the point.

`lifetime_rating` reads `v_counted_attempt`, which excludes self-authored
practice — a rating must not move because a rep rehearsed privately.

`total_completed_drills` counts EVERY graded attempt, self-authored included. The
contract keeps two vocabularies deliberately: every manager-facing figure is a
`counted_attempts` (see `RepDeepDive`), and `total_completed_drills` appears in
exactly one schema, `ProfileView`, returned by exactly one owner-only operation.

So there is no concealment to protect here — AC-TRP-004 hides a rep's practice
from their MANAGER, not from themselves — and excluding it would just under-report
a rep's own effort back to them.
"""

_BADGES = text(
    """
    select b.code, b.name, b.rule_text,
           w.account_id is not null as earned,
           w.earned_at
    from badge b
    left join badge_award w on w.badge_code = b.code and w.account_id = :account
    where b.active
    order by (w.account_id is null), b.code
    """
)
"""Every active badge, earned or not — locked ones carry the goal (FR-TRP-005).

A `left join` rather than selecting awards only: the profile shows what is still
reachable, and a list of achievements alone would be useless to the rep who has
none yet. Earned sort first.
"""


async def profile(session: AsyncSession, *, account_id: UUID) -> ProfileView:
    """The rep's own profile. Owner-only — badges appear on no other surface."""
    row = (await session.execute(_PROFILE, {"account": account_id})).one_or_none()
    if row is None:
        raise ProblemError(catalog.SESSION_INVALID)

    badges = (await session.execute(_BADGES, {"account": account_id})).all()
    return ProfileView(
        account_id=account_id,
        display_name=row.display_name,
        email=row.email,
        total_completed_drills=int(row.graded),
        lifetime_rating=_score_band(row.rating, row.band),
        badges=[
            BadgeView(
                code=b.code,
                name=b.name,
                rule_text=b.rule_text,
                earned=b.earned,
                earned_at=b.earned_at,
            )
            for b in badges
        ],
    )


# ── GET /me/coach-feedback · POST /me/coach-feedback/mark-read ────────────────

_FEEDBACK = text(
    """
    select f.id, f.body, f.created_at, f.read_at is not null as read
    from coach_feedback_item f
    where f.rep_account_id = :account
      and (cast(:cursor as timestamptz) is null or f.created_at < :cursor)
    order by f.created_at desc
    limit :limit
    """
)
"""Keyset pagination on `created_at`, not OFFSET.

The feed grows at the head, so an offset page shifts under the reader between
requests and silently repeats or skips an item. The cursor is the timestamp of
the last row returned, which `idx_feedback_rep` already orders by.
"""

_UNREAD = text(
    "select count(*) from coach_feedback_item"
    " where rep_account_id = :account and read_at is null"
)

_MARK_READ = text(
    """
    update coach_feedback_item f
    set read_at = pg_catalog.now()
    where f.rep_account_id = :account
      and f.read_at is null
      and f.created_at <= (
          select c.created_at from coach_feedback_item c
          where c.id = :through and c.rep_account_id = :account
      )
    returning 1
    """
)
"""Marks the named item and everything older.

The subquery is scoped to the reader as well as to the id. Without that second
predicate an id belonging to somebody else would resolve to THEIR timestamp and
mark this reader's feed read against a stranger's clock — RLS would not catch it,
because every row actually written still belongs to the caller.
"""

_OWNS = text(
    "select exists (select 1 from coach_feedback_item"
    " where id = :id and rep_account_id = :account)"
)


def encode_cursor(at: datetime) -> str:
    """The keyset position, as an opaque token.

    The contract types this `string` and calls it opaque, so it must not be a
    readable timestamp. That is not decoration: a client that can read the cursor
    will eventually construct one, and then the ordering column — `created_at`
    today — is part of the public API and cannot be changed without breaking
    every caller. Base64 makes the boundary obvious to anyone tempted.

    Not encryption, and not pretending to be. A determined client can decode it;
    the point is that nothing about the response invites them to.
    """
    return urlsafe_b64encode(at.isoformat().encode()).decode()


def decode_cursor(cursor: str | None) -> datetime | None:
    """Read a cursor back, or refuse it.

    Raises:
        ProblemError: `422 validation-error` on anything this did not mint.
            Letting a malformed cursor through as `None` would silently restart
            the reader at the head of the feed — a pagination loop that looks like
            data rather than an error.
    """
    if cursor is None:
        return None
    try:
        return datetime.fromisoformat(urlsafe_b64decode(cursor.encode()).decode())
    except (ValueError, TypeError, BinasciiError) as exc:
        raise ProblemError(
            catalog.VALIDATION_ERROR, detail="cursor is not one this server issued"
        ) from exc


@dataclass(frozen=True, slots=True)
class _LibraryCursor:
    """A position in the library's ordering, which is not a single column.

    `recommended` is three groups stitched together — assigned by due date, then
    unattempted drills newest-published, then everything else by recency — so a
    position in it needs the group, the key within the group, and a tiebreak. That
    is the whole reason this exists rather than reusing `encode_cursor`: a bare
    timestamp cannot say which of the three groups it belongs to, and the same
    timestamp legitimately appears in more than one.

    `drill_id` is the tiebreak and it is not optional. Two drills published in the
    same transaction share a `published_at` to the microsecond, and a keyset with
    a non-unique key either repeats that pair forever or skips one of them.

    `sort` rides along so a client that changes ordering mid-page is refused
    rather than silently served nonsense: the bucket numbers mean different things
    under each ordering, so `recommended`'s bucket 1 read as `recently_assigned`'s
    bucket 1 lands the reader somewhere arbitrary. That failure looks like missing
    data, not like an error, which is why it is worth a field.
    """

    sort: LibrarySort
    bucket: int
    sort_key: Decimal
    drill_id: UUID

    MAX_BUCKET = 3
    """The highest group `recommended` emits — assigned, unattempted, attempted,
    drafts. `recently_assigned` uses only 0 and 1, so this is the looser of the
    two and both are covered by it."""

    def encode(self) -> str:
        """Base64, for the same reason `encode_cursor` uses it: the contract types
        this `string` and calls it opaque, and a client that can read a cursor
        eventually constructs one — at which point the ordering is public API."""
        raw = f"{self.sort}|{self.bucket}|{self.sort_key}|{self.drill_id}"
        return urlsafe_b64encode(raw.encode()).decode()

    @classmethod
    def decode(cls, cursor: str, *, sort: LibrarySort) -> _LibraryCursor:
        """Read a cursor back, or refuse it.

        Raises:
            ProblemError: `422 validation-error` on anything this did not mint,
                and on one this did mint under a different ordering. Both are the
                same defect from the reader's side — the position is not
                interpretable — and letting either through would restart them at
                the head of the grid, which reads as data rather than as an error.

        Parsing is not validation, and the difference is a `500`. `int()` accepts
        an integer of any width, so a crafted bucket sails through here and fails
        later as an `asyncpg.DataError` — "value out of int32 range" — raised from
        inside query execution, well past the point where this refusal could
        happen. `Decimal` is the same story for `nan` and `inf`, which parse
        happily and then compare as nothing sensible.

        So the bounds below are what make the refusal reachable: every value is
        checked against the range this class can actually MINT, not against what
        its type can hold.
        """
        try:
            kind, bucket, key, drill = urlsafe_b64decode(cursor.encode()).decode().split("|")
            decoded = cls(
                sort=cast(LibrarySort, kind),
                bucket=int(bucket),
                sort_key=Decimal(key),
                drill_id=UUID(drill),
            )
        except (ValueError, TypeError, BinasciiError, InvalidOperation) as exc:
            raise ProblemError(
                catalog.VALIDATION_ERROR, detail="cursor is not one this server issued"
            ) from exc
        if not 0 <= decoded.bucket <= cls.MAX_BUCKET or not decoded.sort_key.is_finite():
            raise ProblemError(
                catalog.VALIDATION_ERROR, detail="cursor is not one this server issued"
            )
        if decoded.sort != sort:
            raise ProblemError(
                catalog.VALIDATION_ERROR,
                detail=f"cursor belongs to sort={decoded.sort!r}, not {sort!r}",
            )
        return decoded


async def coach_feedback(
    session: AsyncSession, *, account_id: UUID, cursor: str | None, limit: int
) -> CoachFeedbackPage:
    """The coaching feed, newest first, with the unread badge count.

    Args:
        session: A transaction scoped to the reading rep.
        account_id: From the session record.
        cursor: The opaque token from the previous page, or None for the first.
        limit: Page size, already bounded by the router.

    Raises:
        ProblemError: `422 validation-error` on a cursor this server did not mint.

    Returns:
        One page plus the unread count — the only notification badge in v1.
    """
    rows = (
        await session.execute(
            _FEEDBACK,
            {"account": account_id, "cursor": decode_cursor(cursor), "limit": limit + 1},
        )
    ).all()
    # One row beyond the page is fetched purely to answer `has_more` without a
    # second COUNT over a feed that only grows.
    has_more = len(rows) > limit
    page = rows[:limit]
    unread = (await session.execute(_UNREAD, {"account": account_id})).scalar_one()

    return CoachFeedbackPage(
        data=[
            CoachFeedbackItemView(id=r.id, body=r.body, created_at=r.created_at, read=r.read)
            for r in page
        ],
        unread_count=int(unread),
        pagination=CursorPage(
            next_cursor=encode_cursor(page[-1].created_at) if page and has_more else None,
            has_more=has_more,
        ),
    )


# ── GET /me/library ───────────────────────────────────────────────────────────

_LIBRARY_UNIVERSE = """
with visible as (
    -- The library universe, stated rather than left to RLS.
    --
    -- For a REP the policies already produce exactly this, so these predicates
    -- are defence in depth. For a MANAGER they are load-bearing: P3 hands back
    -- every team draft, so without them a manager calling /me/library would list
    -- other people's unpublished work. Manager drafts belong to GET /team/drills
    -- (FR-DRL-013); only the caller's OWN self-authored drafts appear here.
    --
    -- `team_id` is on the PUBLISHED branch ONLY, and the asymmetry is deliberate.
    --
    -- On that branch it buys an index: RLS reaches team through
    -- `app_drill_readable_published`, a SECURITY DEFINER function the planner
    -- cannot see into, so without this line the only visible predicate is
    -- `org_id` and the scan is org-wide — `ix_drill_org_id` instead of
    -- `idx_drill_team_status`, which data/02 §2 names for this surface.
    --
    -- On the self-authored branch the same line would be a BUG. A rep's own
    -- practice is theirs, not their team's: `app_drill_authored_by_account`
    -- matches on org and author and says nothing about team, so after a transfer
    -- a team predicate here would delete the rep's private drills from their own
    -- library. Assignments follow the team; self-authored practice follows the
    -- person.
    -- UNION ALL, not OR. data/02 §2 writes this surface as a union — "team
    -- published drills ∪ assigned ∪ own self-authored" — and names an index per
    -- branch. A single OR cannot use either: PostgreSQL satisfies one index cond
    -- per scan, so an OR across two differently-shaped branches collapses to the
    -- widest common predicate (org_id) with everything else as a filter. Two
    -- selects let each branch take its own index.
    --
    -- ALL rather than DISTINCT because the branches are provably disjoint — one
    -- requires `not self_authored`, the other requires it — so deduplicating
    -- would be a sort over rows that cannot collide.
    select d.id, d.status, d.label, d.call_type, d.lead_type,
           d.self_authored, d.published_at, d.created_at
    from drill d
    where d.status = 'published' and not d.self_authored and d.team_id = :team
    union all
    select d.id, d.status, d.label, d.call_type, d.lead_type,
           d.self_authored, d.published_at, d.created_at
    from drill d
    where d.self_authored and d.author_account_id = :account
      and d.status <> 'archived'   -- archive is withdrawal (FR-DRL-016)
),
carded as (
    select v.id, v.status, v.label, v.call_type, v.lead_type, v.self_authored,
           v.published_at, v.created_at,
           asg.due_date, asg.attempts_used, asg.attempts_allowed, asg.granted_at,
           st.best,
           exists (select 1 from attempt t
                    where t.drill_id = v.id
                      and t.rep_account_id = :account) as attempted
    from visible v
    -- One lateral, not two left joins. Joining `assignment` on its own would
    -- surface a due date on a card the caller is not a recipient of — the
    -- assignment exists, they just are not party to it.
    left join lateral (
        select a.due_date, a.attempts_allowed, ar.attempts_used, ar.granted_at
        from assignment_recipient ar
        join assignment a on a.id = ar.assignment_id
        where a.drill_id = v.id and ar.rep_account_id = :account
    ) asg on true
    -- V-8 is security_invoker, so a manager reading it sees their whole team.
    -- The account predicate is what stops a manager's /me reporting the team's
    -- best as their own.
    left join v_participant_drill_stats st
           on st.drill_id = v.id and st.rep_account_id = :account
),
filtered as (
    select * from carded c
    where (cast(:call_type as text) is null or c.call_type = :call_type)
      and (:attempted = 'all'
           or (:attempted = 'attempted'   and c.attempted)
           or (:attempted = 'unattempted' and not c.attempted))
),
ordered as (
    select f.*,
           case when f.due_date is not null then 'assigned'
                when f.self_authored        then 'self_authored'
                else 'library' end as source,
           -- Buckets, per FR-TRP-008's `recommended`: assigned first by due date,
           -- then unattempted, then the rest. Drafts get a bucket of their own at
           -- the end rather than sorting by recency among practicable cards — a
           -- freshly created draft would otherwise lead the grid, and it is the
           -- one card that cannot be started (409 drill-not-startable).
           case
             when :sort = 'recently_assigned' then
                  case when f.due_date is not null then 0 else 1 end
             else case when f.due_date is not null                    then 0
                       when f.status = 'published' and not f.attempted then 1
                       when f.status = 'published'                     then 2
                       else 3 end
           end as bucket,
           -- One ascending numeric key across every bucket, because a keyset
           -- comparison cannot mix directions. Descending orders are negated
           -- rather than expressed as DESC: `(bucket, sort_key, id) > (...)` is a
           -- single row comparison, and it only means "later in the ordering" if
           -- every component ascends.
           case
             when :sort = 'recently_assigned' then
                  case when f.due_date is not null
                       then -extract(epoch from f.granted_at)
                       else -extract(epoch from coalesce(f.published_at, f.created_at)) end
             else case when f.due_date is not null
                       then  extract(epoch from f.due_date)
                       else -extract(epoch from coalesce(f.published_at, f.created_at)) end
           end::numeric as sort_key
    from filtered f
)
"""
"""The shared CTE chain. Both statements below open with it.

Composed rather than duplicated: the universe, the concealment predicates and the
bucket arithmetic have one definition, and a change to any of them cannot land in
the page while missing the counts.
"""

_LIBRARY_PAGE = text(
    _LIBRARY_UNIVERSE
    + """
    select o.id, o.status, o.label, o.call_type, o.lead_type, o.source,
           o.attempted, o.best,
           case when o.best is null then null else fn_score_band(o.best) end as band,
           o.due_date, o.attempts_used, o.attempts_allowed,
           o.bucket, o.sort_key
    from ordered o
    where (:source = 'all'
           or (:source = 'assigned'      and o.source = 'assigned')
           or (:source = 'self_authored' and o.source = 'self_authored'))
      and (cast(:cursor_bucket as int) is null
           or (o.bucket, o.sort_key, o.id)
               > (:cursor_bucket, cast(:cursor_key as numeric), cast(:cursor_drill as uuid)))
    order by o.bucket, o.sort_key, o.id
    limit :limit
    """
)
"""`fn_score_band` is guarded because it answers `'red'` for NULL — `null >= 7.5`
is null, so an unattempted drill would come back banded red rather than unbanded.
The Python side would catch it, but a column that lies is a trap for the next
reader of this query."""

_LIBRARY_COUNTS = text(
    _LIBRARY_UNIVERSE
    + """
    select count(*)                                        as all_count,
           count(*) filter (where source = 'assigned')      as assigned_count,
           count(*) filter (where source = 'self_authored') as self_count
    from ordered
    """
)
"""Separate statement, and deliberately not a window function on the page above.

Two reasons, either sufficient. `WHERE` runs before window functions, so a
`count(*) over ()` there would count only the rows surviving the source filter —
every tab would report its own size and the other two would read zero. And a
`CROSS JOIN` onto the page loses the counts entirely when the page is empty,
which is exactly when a rep most needs to see that the other tabs are not."""


def _library_card(row: Any) -> LibraryCard:
    """One row to one card. `assignment` is present iff a due date came back,
    which is iff the caller holds an allowance on it."""
    return LibraryCard(
        drill_id=row.id,
        status=row.status,
        label=row.label,
        call_type=row.call_type,
        lead_type=row.lead_type,
        source=cast(LibrarySource, row.source),
        attempted=row.attempted,
        best=_score_band(row.best, row.band),
        assignment=(
            LibraryAssignment(
                due_date=row.due_date,
                attempts_used=row.attempts_used,
                attempts_allowed=row.attempts_allowed,
                # Computed here, not in SQL, only because it is arithmetic on two
                # columns already being returned. FR-TRP-013's rule lives in one
                # place either way.
                locked=row.attempts_used >= row.attempts_allowed,
            )
            if row.due_date is not None
            else None
        ),
    )


async def library(
    session: AsyncSession,
    *,
    account_id: UUID,
    team_id: UUID,
    source: SourceFilter,
    call_type: CallType | None,
    attempted: AttemptedFilter,
    sort: LibrarySort,
    cursor: str | None,
    limit: int,
) -> LibraryPage:
    """Everything the rep can practice, as one grid (FR-TRP-006).

    Args:
        session: A transaction scoped to the caller.
        account_id: From the session record, never from the request.
        team_id: Also from the session record. Narrows the team-library branch to
            an index; it does NOT decide visibility, which RLS already settled.
        source: Which tab (FR-TRP-007). Narrows the page, NOT the counts.
        call_type: Optional refinement (FR-TRP-008).
        attempted: Optional refinement (FR-TRP-008).
        sort: Ordering. The cursor is bound to it and refuses a mismatch.
        cursor: Opaque position from a previous page.
        limit: Page size, already bounded by the router.

    Raises:
        ProblemError: `422 validation-error` on a cursor this server did not mint,
            or one minted under a different `sort`.

    Returns:
        The page, its per-tab counts, and the position to resume from.
    """
    position = _LibraryCursor.decode(cursor, sort=sort) if cursor is not None else None
    shared = {
        "account": account_id,
        "team": team_id,
        "call_type": call_type,
        "attempted": attempted,
        "sort": sort,
    }

    rows = (
        await session.execute(
            _LIBRARY_PAGE,
            {
                **shared,
                "source": source,
                "cursor_bucket": position.bucket if position else None,
                "cursor_key": str(position.sort_key) if position else None,
                "cursor_drill": str(position.drill_id) if position else None,
                # One row beyond the page answers `has_more` without a second
                # count over a grid that the source filter has already narrowed.
                "limit": limit + 1,
            },
        )
    ).all()
    totals = (await session.execute(_LIBRARY_COUNTS, shared)).one()

    has_more = len(rows) > limit
    page = rows[:limit]
    last = page[-1] if page else None

    return LibraryPage(
        data=[_library_card(row) for row in page],
        counts=LibraryCounts(
            all=int(totals.all_count),
            assigned=int(totals.assigned_count),
            self_authored=int(totals.self_count),
        ),
        pagination=CursorPage(
            next_cursor=(
                _LibraryCursor(
                    sort=sort, bucket=last.bucket, sort_key=last.sort_key, drill_id=last.id
                ).encode()
                if last is not None and has_more
                else None
            ),
            has_more=has_more,
        ),
    )


# ── GET /drills/{drill_id}/my-history ─────────────────────────────────────────

_DRILL_VISIBLE = text("select exists (select 1 from drill d where d.id = :drill)")
"""Visibility, left entirely to RLS — and this is the one query on this surface
that does NOT restate the policies.

The library narrows what the policies return, because a grid is a curated set:
archived is withdrawn, other people's drafts are not the caller's business. A
history is the opposite. FR-DRL-016 archives a drill *with history preserved*
(AC-DRL-007), so an archived drill must still answer here — the same word that
means "filter it out" one surface over means "keep every record" on this one.

What remains is exactly the policy question: can this caller see this drill at
all? RLS already answers it, so asking again in SQL would be a second, drifting
copy of a rule that has one home.
"""

_DRILL_STATS = text(
    """
    select st.best, st.latest, st.average, st.trend, st.graded_attempts
    from v_participant_drill_stats st
    where st.drill_id = :drill and st.rep_account_id = :account
    """
)
"""V-8 — the ONE definition of these four (ADR-0034), so the number here and the
number on a manager's surface cannot disagree.

`rep_account_id = :account` is load-bearing, not defensive. The view is
`security_invoker`, so a MANAGER reading it sees their whole team: without this
predicate their own history on a drill their reps have practised would report the
team's best as theirs. That is the `/me` trap, and it is worst here because the
answer looks entirely plausible."""

_DRILL_ATTEMPTS = text(
    """
    select a.id as attempt_id,
           row_number() over (order by a.started_at, a.id) as attempt_number,
           a.started_at, a.status,
           sc.overall_score,
           case when sc.overall_score is null then null
                else fn_score_band(sc.overall_score) end as band
    from attempt a
    left join scorecard sc on sc.attempt_id = a.id
    where a.drill_id = :drill and a.rep_account_id = :account
    order by a.started_at desc, a.id desc
    """
)
"""Every attempt, graded or not — a different set from the statistics above.

`attempt_number` counts from the OLDEST while the rows come back newest first.
Both orderings are correct and they are deliberately opposed: FR-TRP-011 lists
newest first, FR-TRP-012's chart reverses that list and reads the numbers as its
x-axis. Numbering by output position would give the newest attempt number 1.

`a.id` tiebreaks both. Attempts share a `started_at` only if two began in the
same microsecond, but a window function with a non-unique ORDER BY assigns
numbers arbitrarily within the tie, and UUIDv7 is time-ordered so the tiebreak
agrees with the clock instead of fighting it.

`left join scorecard` rather than a join: an interrupted or in-progress attempt
has no scorecard and must still be listed. An inner join would silently drop
exactly the attempts a rep is most likely to be asking about."""


async def drill_history(
    session: AsyncSession, *, account_id: UUID, drill_id: UUID
) -> DrillHistory:
    """The caller's own history on one drill (FR-TRP-011, FR-SCR-015).

    Args:
        session: A transaction scoped to the caller.
        account_id: From the session record, never from the request.
        drill_id: From the path.

    Raises:
        ProblemError: `404 not-found` when the drill does not exist OR the caller
            cannot see it. The two answer identically (ADR-0036): distinguishing
            them would make this a probe for which drill ids exist, and — since
            self-authored drills are private — for whose private practice is
            whose.

    Returns:
        The statistics primitives and the attempt list. A drill the caller can see
        but has never attempted returns null statistics and an empty list, which
        is an answer rather than an error.
    """
    if not (await session.execute(_DRILL_VISIBLE, {"drill": drill_id})).scalar_one():
        raise ProblemError(catalog.NOT_FOUND)

    params = {"drill": drill_id, "account": account_id}
    stats = (await session.execute(_DRILL_STATS, params)).one_or_none()
    rows = (await session.execute(_DRILL_ATTEMPTS, params)).all()

    return DrillHistory(
        drill_id=drill_id,
        # No row from V-8 means no GRADED attempts — the view aggregates only
        # those. It does not mean no attempts, which is why the list below is
        # read independently rather than derived from this.
        stats=DrillStats(
            best=float(stats.best) if stats and stats.best is not None else None,
            latest=float(stats.latest) if stats and stats.latest is not None else None,
            average=float(stats.average) if stats and stats.average is not None else None,
            trend=float(stats.trend) if stats and stats.trend is not None else None,
            graded_attempts=int(stats.graded_attempts) if stats else 0,
        ),
        attempts=[
            AttemptSummary(
                attempt_id=row.attempt_id,
                attempt_number=int(row.attempt_number),
                started_at=row.started_at,
                status=row.status,
                score=_score_band(row.overall_score, row.band),
            )
            for row in rows
        ],
    )


async def mark_feedback_read(
    session: AsyncSession, *, account_id: UUID, through_id: UUID
) -> MarkReadResult:
    """Mark the named item and every older one read.

    Args:
        session: A transaction scoped to the reading rep.
        account_id: From the session record.
        through_id: The newest item the reader has seen.

    Raises:
        ProblemError: `404 not-found` if the id is not this reader's. Absence and
            "belongs to someone else" answer identically — distinguishing them
            would turn this into a probe for which feedback ids exist (ADR-0036,
            denial as absence).

    Returns:
        The unread count AFTER the write, so the client updates its badge from
        the response rather than re-fetching the page.

    An update that changes nothing has two causes: the id is not theirs, or
    everything through it was already read. Only the first is a `404` — the
    second is an ordinary replay and stays a success.
    """
    # `returning 1` and count the rows, rather than `result.rowcount`: SQLAlchemy
    # types the async `execute` as `Result[Any]`, which has no `rowcount`, and the
    # repo already carries several mypy errors from reaching for it anyway.
    marked = (
        await session.execute(_MARK_READ, {"account": account_id, "through": through_id})
    ).all()
    if not marked:
        owned = (
            await session.execute(_OWNS, {"id": through_id, "account": account_id})
        ).scalar_one()
        if not owned:
            raise ProblemError(catalog.NOT_FOUND)

    unread = (await session.execute(_UNREAD, {"account": account_id})).scalar_one()
    return MarkReadResult(unread_count=int(unread))


# ── GET /team/dashboard ───────────────────────────────────────────────────────

HIGH_GAP = 2.0
MODERATE_GAP = 1.0
"""FR-TRM-004's severity thresholds, and the only copy of them.

V-6 returns the gap and stops — the marking is a presentation decision, so it
lives with the response rather than in the derivation. Both are `>=`: a gap of
exactly 2.0 is high, and the criterion says "≥ 2.0", not "greater than".
"""

_TEAM_MONTH = text(
    """
    select cur.team_average,
           fn_score_band(cur.team_average)                   as band,
           cur.top_performers,
           cur.needs_coaching,
           cur.rated_reps,
           round(cur.team_average - prev.team_average, 1)     as delta
    from v_team_month cur
    left join v_team_month prev
      on prev.team_id = cur.team_id
     and prev.month   = cur.month - interval '1 month'
    where cur.team_id = :team and cur.month = :month
    """
)
"""The team's month, with the prior month's average beside it.

`left join`, so a first measured month still returns its row and `delta` comes
back null. An inner join would drop the whole dashboard for a team that has only
ever been measured once — the exact team most likely to be looking at it.

The prior month is `cur.month - interval '1 month'`, the same expression V-3 uses
for a rep's trend, so "prior month" means one civil month back in the org's
calendar rather than 30 days.

`team_id = :team` is explicit even though RLS already scopes the read. Here that
is defence in depth rather than the load-bearing filter it is on `/me`: a manager
is SUPPOSED to see their whole team, so the policies would answer correctly
anyway — but the query then states its own scope instead of inheriting it, and a
policy that ever widened to the org would not silently widen this.
"""

_GAP_ANALYSIS = text(
    """
    select call_type, top_avg, bottom_half_avg, gap
    from v_gap_analysis
    where team_id = :team and month = :month
    order by gap desc nulls last, call_type
    """
)
"""Widest gap first (FR-TRM-004, AC-TRM-002).

`nulls last` is not decoration. PostgreSQL sorts NULLs FIRST under `desc` by
default, so a call type whose gap could not be computed would head a list whose
entire purpose is "worst first" — the coach's eye lands on the one row carrying
no finding.

`call_type` breaks ties, so two equal gaps come back in a stable order rather
than whichever the scan happened to produce.
"""


def _severity(gap: Decimal | float | None) -> Severity:
    """Mark a gap, guarding the null rather than comparing it.

    `None >= 2.0` raises `TypeError` in Python — unlike SQL, where `null >= 2.0`
    is null and quietly falls through. A gap is null whenever a cohort has no
    attempts on that call type, which is ordinary, so this is a live path and not
    a defensive gesture.

    Both comparisons are `>=`. FR-TRM-004 says "≥ 2.0 high, ≥ 1.0 moderate", so a
    gap of exactly 2.0 is high — the boundary a `>` would silently move, and the
    reason `tests/l1_unit/test_gap_severity.py` asserts the thresholds themselves
    rather than only the values a fixture happens to produce.
    """
    if gap is None:
        return "none"
    value = float(gap)
    if value >= HIGH_GAP:
        return "high"
    if value >= MODERATE_GAP:
        return "moderate"
    return "none"


def _optional_float(value: Decimal | float | None) -> float | None:
    """`numeric` arrives as `Decimal`; the contract types these as plain numbers."""
    return None if value is None else float(value)


async def team_dashboard(
    session: AsyncSession, *, account_id: UUID, team_id: UUID, month: str | None
) -> TeamDashboard:
    """The month's coaching picture (FR-TRM-004, AC-TRM-001/002).

    Args:
        session: A scoped session; both reads are RLS-filtered.
        account_id: The manager, whose org supplies the calendar.
        team_id: Their team, from the session record rather than the request.
        month: `YYYY-MM`, or `None` for the org's current month.

    Returns:
        Every contracted field populated, including on a month with no attempts —
        where the team average is null rather than a banded zero, and tiering
        reads as suppressed because zero rated reps is fewer than five.

    Raises:
        ValidationProblem: `422` if `month` is not a calendar month.
        ProblemError: `401 session-invalid` if the account is gone.
    """
    anchor = await resolve_anchor(session, account_id=account_id, month=month)
    bind = {"team": team_id, "month": anchor.month}

    row = (await session.execute(_TEAM_MONTH, bind)).one_or_none()
    gaps = (await session.execute(_GAP_ANALYSIS, bind)).all()

    return TeamDashboard(
        month=anchor.month.strftime("%Y-%m"),
        # No row means nobody on the team was rated this month. That is an empty
        # month, not an error and not a zero: `_score_band` already refuses to
        # band a null, so the alternative — reading `fn_score_band(null)` — never
        # gets the chance to report it as red.
        team_average=None if row is None else _score_band(row.team_average, row.band),
        delta=None if row is None else _optional_float(row.delta),
        rated_reps=0 if row is None else int(row.rated_reps),
        # Zero rated reps is fewer than five, so an empty month reads as
        # suppressed — which is true, and is what stops a client rendering "0 top
        # performers" as a finding about the team.
        tiering_suppressed=row is None or int(row.rated_reps) < TIERING_MINIMUM,
        top_performers=0 if row is None else int(row.top_performers),
        needs_coaching=0 if row is None else int(row.needs_coaching),
        gap_analysis=[
            GapRow(
                call_type=gap.call_type,
                top_avg=_optional_float(gap.top_avg),
                bottom_half_avg=_optional_float(gap.bottom_half_avg),
                gap=_optional_float(gap.gap),
                severity=_severity(gap.gap),
            )
            for gap in gaps
        ],
    )


# ── GET /team/roster ──────────────────────────────────────────────────────────

_ROSTER = text(
    """
    select a.id                              as account_id,
           a.display_name,
           t.tier,
           t.rating,
           fn_score_band(t.rating)           as band,
           coalesce(t.counted_attempts, 0)   as counted_attempts,
           t.rating_discovery,
           t.rating_post_proposal,
           t.rating_renewal,
           t.rating_upsell
    from account a
    left join v_rep_month_tier t
      on t.rep_account_id = a.id and t.team_id = :team and t.month = :month
    where a.team_id = :team and a.role = 'rep'
    order by t.rating desc nulls last, a.id
    """
)
"""Every team rep (FR-TRM-005) — which is why this reads FROM `account`.

The tier view holds a row only for a rep with counted attempts, so a roster
selected from it would omit anyone who took no calls this month: a new joiner, or
precisely the person a manager needs to notice. The left join keeps them, with
nulls where there is nothing to report.

`role = 'rep'` excludes the manager, who shares the team's `team_id` — without it
a manager appears on their own roster as an unrated row.

**There is deliberately no `status` filter.** An earlier version had
`status = 'active'`, borrowed from V-7's `eligible_reps` — but that answers a
different question (who may practise) and it silently split this surface from the
one above it. V-1, V-2 and V-4 apply no status filter, so a rep deactivated
mid-month still counts toward `rated_reps` and the team average, their records
being preserved (FR-IDA-010). Filtering here alone put "5 rated reps" on the
dashboard above a four-row roster, with nothing on either to explain the
difference — and FR-TRM-005 positions the roster directly below it.

So "every team rep" is taken literally: whoever carries this `team_id` and the
rep role, which is the same population the derivations count.

`nulls last` for the same reason as the gap rows: PostgreSQL sorts NULLs FIRST
under `desc`, so the unrated rep would otherwise head a list ordered best-first
and read as the strongest performer. `a.id` breaks ties, matching V-4's own
`order by rating desc, rep_account_id` so the roster and the tier ranking cannot
disagree about two reps on the same rating.

`team_id = :team` appears TWICE, and the one in the join earns its place beyond
defence in depth. V-2 groups by `(org_id, team_id, rep_account_id, month)`, so a
rep who moved teams mid-month — `opsChangeTeam` is a contracted operation — has a
row under each team for that month. Joined on `rep_account_id` alone, the left
join matches both and the rep appears on the roster twice.

RLS prevents that today: the manager sees only their own team's attempts, so the
view is computed over one team's rows and yields one row per rep. But that makes
the ROW COUNT a property of a policy rather than of the query. With the predicate
it is structural, and the same holds for `a.team_id` alongside
`account_manager_team_read`.
"""


def _extremes(
    ratings: Mapping[CallType, Decimal | float | None],
) -> tuple[CallType | None, CallType | None]:
    """The rep's best and worst measured call types (FR-TRM-005).

    Untried types are dropped rather than treated as zero — a call type nobody has
    attempted is not a weakness, and counting it as one would make "weakest" mean
    "least practised" on every new rep's row.

    With nothing measured, both are null. With exactly ONE type measured, both name
    it: that type genuinely is their best and their worst, and returning null
    instead would conceal the more useful fact that it is the only one they have.

    Ties resolve to whichever call type comes first in `CALL_TYPES`, because `max`
    and `min` both return the first extreme they meet and the caller builds the
    mapping in that order. Arbitrary, but fixed — two renders of the same month
    must not disagree.
    """
    measured = [(call_type, float(v)) for call_type, v in ratings.items() if v is not None]
    if not measured:
        return None, None
    return (
        max(measured, key=lambda pair: pair[1])[0],
        min(measured, key=lambda pair: pair[1])[0],
    )


async def team_roster(
    session: AsyncSession, *, account_id: UUID, team_id: UUID, month: str | None
) -> TeamRoster:
    """Every active rep on the team, rating descending (FR-TRM-005).

    Args:
        session: A scoped session; the read is RLS-filtered.
        account_id: The manager, whose org supplies the calendar.
        team_id: Their team, from the session record rather than the request.
        month: `YYYY-MM`, or `None` for the org's current month.

    Returns:
        One row per active rep, including those with no counted attempts, who
        carry nulls and sort last.

    Raises:
        ValidationProblem: `422` if `month` is not a calendar month.
        ProblemError: `401 session-invalid` if the account is gone.
    """
    anchor = await resolve_anchor(session, account_id=account_id, month=month)
    rows = (
        await session.execute(_ROSTER, {"team": team_id, "month": anchor.month})
    ).all()

    roster: list[RosterRow] = []
    for row in rows:
        # Built in CALL_TYPES order, which is what makes the tie-break in
        # `_extremes` deterministic rather than dependent on column order.
        strongest, weakest = _extremes(
            {
                "discovery": row.rating_discovery,
                "post_proposal": row.rating_post_proposal,
                "renewal": row.rating_renewal,
                "upsell": row.rating_upsell,
            }
        )
        roster.append(
            RosterRow(
                account_id=row.account_id,
                display_name=row.display_name,
                tier=row.tier,
                # `fn_score_band(null)` is `'red'`, so an unrated rep would arrive
                # banded red on a null score. `_score_band` refuses the pair.
                rating=_score_band(row.rating, row.band),
                counted_attempts=int(row.counted_attempts),
                strongest_call_type=strongest,
                weakest_call_type=weakest,
            )
        )

    return TeamRoster(month=anchor.month.strftime("%Y-%m"), data=roster)


# ── GET /team/reps/{account_id} ───────────────────────────────────────────────

_DEEP_DIVE_REP = text(
    """
    select a.display_name,
           t.rating,
           fn_score_band(t.rating)                as band,
           t.trend,
           t.rating_discovery,
           fn_score_band(t.rating_discovery)      as band_discovery,
           t.rating_post_proposal,
           fn_score_band(t.rating_post_proposal)  as band_post_proposal,
           t.rating_renewal,
           fn_score_band(t.rating_renewal)        as band_renewal,
           t.rating_upsell,
           fn_score_band(t.rating_upsell)         as band_upsell
    from account a
    left join v_rep_month_trend t
      on t.rep_account_id = a.id and t.team_id = :team and t.month = :month
    where a.id = :rep and a.team_id = :team and a.role = 'rep'
    """
)
"""The rep's identity and month, and the gate on whether they may be asked about.

Selected FROM `account` with the team predicate, so a rep on another team returns
NO ROW rather than a row with empty numbers — which is what lets the caller answer
`404` on the same path as a genuinely absent id. `role = 'rep'` refuses a
manager's own id for the same reason: a deep dive of a manager is not a thing this
surface describes, and an empty profile would read as a rep who never practised.

`left join`, so a rep with no counted attempts this month still resolves and
answers with nulls. An inner join would make "no attempts yet" indistinguishable
from "not your rep", which is the one distinction this endpoint must get right in
both directions.

The four `rating_<type>` columns come from V-2 through V-3 — they are the
per-call-type profile, read rather than recomputed. An earlier version derived
them here with its own `round(avg(overall_score), 1)` grouped by call type: the
same arithmetic today, and a SECOND DEFINITION of a canonical formula, which
data/02 §3 forbids for exactly the reason it matters here. The roster reads those
columns through V-4 and `/me/progress` reads them through V-2; a private copy
would agree with both right up until V-2 changed, and then this surface alone
would disagree with the two it is supposed to render "reframed" (FR-TRP-002).
"""

_DEEP_DIVE_COUNTS = text(
    """
    select call_type, count(*) as counted_attempts
    from v_counted_attempt
    where rep_account_id = :rep and team_id = :team and local_month = :month
    group by call_type
    """
)
"""Attempts per call type — the one thing V-2 does not carry.

V-2 exposes `rating_<type>` but only a TOTAL `counted_attempts`, and the contract
asks for a count beside each rating. So this supplies counts and nothing else:
the ratings come from the view, and no formula is written twice.

Counting straight off `v_counted_attempt` keeps the two consistent by
construction — a count over the pool the ratings were averaged from, so a rating
can never appear beside a count drawn from a different set. Only attempted types
come back; the caller fills the remainder with zero.
"""

_DEEP_DIVE_ATTEMPTS = text(
    """
    with numbered as (
        select a.id as attempt_id,
               row_number() over (
                   partition by a.drill_id order by a.started_at, a.id
               ) as attempt_number
        from attempt a
        where a.rep_account_id = :rep and a.team_id = :team
    )
    select v.attempt_id,
           d.label                        as drill_label,
           v.call_type,
           v.started_at,
           v.overall_score,
           fn_score_band(v.overall_score) as band,
           n.attempt_number
    from v_counted_attempt v
    join numbered n on n.attempt_id = v.attempt_id
    join drill d    on d.id = v.drill_id
    where v.rep_account_id = :rep and v.team_id = :team and v.local_month = :month
    order by v.started_at desc, v.attempt_id desc
    """
)
"""This month's counted attempts, newest first, each numbered within its drill.

## Why the numbering is not month-scoped

`numbered` deliberately spans every attempt the rep has on each drill, and the
month filter is applied afterwards. Partitioning over the month instead would
restart each drill at 1 — self-consistent, and disagreeing with the number the rep
reads for the same attempt in their own drill history, which FR-TRP-002 forbids
and FR-TRM-007 puts in the replay chrome.

It also counts attempts that are NOT in the counted pool: an interrupted attempt
between two graded ones occupies a number, exactly as it does on the rep's
surface. Numbering only the counted ones would close the gap and produce a
different sequence from the one the rep sees.

`a.id` tiebreaks, as in `_DRILL_ATTEMPTS`: two attempts share a `started_at` only
within a microsecond, but a window function with a non-unique ORDER BY assigns
numbers arbitrarily inside a tie, and UUIDv7 is time-ordered so the tiebreak
agrees with the clock.

## No cap, deliberately

The contract offers no pagination here, so any limit would be a silent
truncation — a coaching surface quietly hiding a rep's attempts from their
manager. The month already bounds the set, and attempts are bounded within it by
assignment allowances. If this ever needs a ceiling it needs a contract change,
not a constant.
"""


async def rep_deep_dive(
    session: AsyncSession,
    *,
    account_id: UUID,
    team_id: UUID,
    rep_account_id: UUID,
    month: str | None,
) -> RepDeepDive:
    """One rep's month, in the manager's framing (FR-TRM-006).

    Args:
        session: A scoped session; every read is RLS-filtered.
        account_id: The manager, whose org supplies the calendar.
        team_id: Their team, from the session record rather than the request.
        rep_account_id: The rep asked about — the one value here that IS
            caller-supplied, and therefore the one the team predicate exists for.
        month: `YYYY-MM`, or `None` for the org's current month.

    Returns:
        The rep's rating and trend, all four call types, and this month's counted
        attempts newest first.

    Raises:
        ProblemError: `404 not-found` when the id is not a rep on this manager's
            team — whether it belongs to another team, to a manager, or to nobody.
            Never `403`: that would confirm the rep exists (AC-IDA-006).
        ValidationProblem: `422` if `month` is not a calendar month.
    """
    anchor = await resolve_anchor(session, account_id=account_id, month=month)
    bind = {"rep": rep_account_id, "team": team_id, "month": anchor.month}

    rep = (await session.execute(_DEEP_DIVE_REP, bind)).one_or_none()
    if rep is None:
        raise not_found()

    counts = {
        row.call_type: int(row.counted_attempts)
        for row in (await session.execute(_DEEP_DIVE_COUNTS, bind)).all()
    }
    attempts = (await session.execute(_DEEP_DIVE_ATTEMPTS, bind)).all()

    # Read off V-2 (through V-3), not recomputed. A rep with no row at all leaves
    # every one of these null, which `_score_band` renders as an unrated type
    # rather than a red one.
    rated: Mapping[CallType, tuple[object, object]] = {
        "discovery": (rep.rating_discovery, rep.band_discovery),
        "post_proposal": (rep.rating_post_proposal, rep.band_post_proposal),
        "renewal": (rep.rating_renewal, rep.band_renewal),
        "upsell": (rep.rating_upsell, rep.band_upsell),
    }

    return RepDeepDive(
        account_id=rep_account_id,
        display_name=rep.display_name,
        month=anchor.month.strftime("%Y-%m"),
        # `fn_score_band(null)` is `'red'`; `_score_band` refuses the pair, so a
        # rep with no counted attempts is unrated rather than alarmingly red.
        rating=_score_band(rep.rating, rep.band),
        trend=_optional_float(rep.trend),
        per_call_type=[
            CallTypeProfile(
                call_type=call_type,
                rating=_score_band(*rated[call_type]),
                counted_attempts=counts.get(call_type, 0),
            )
            # CALL_TYPES, not the query's grouping — all four appear, in a fixed
            # order, so a manager comparing two reps reads the same columns in the
            # same places.
            for call_type in CALL_TYPES
        ],
        recent_attempts=[
            RecentAttempt(
                attempt_id=row.attempt_id,
                drill_label=row.drill_label,
                call_type=row.call_type,
                started_at=row.started_at,
                # Never null: the counted pool is graded attempts only, so the
                # contract types this non-nullable and the data agrees.
                score=ScoreBand(score=float(row.overall_score), band=row.band),
                attempt_number=int(row.attempt_number),
            )
            for row in attempts
        ],
    )


# ── GET /team/drills ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _CatalogCursor:
    """A keyset position in the catalog: `(updated_at, drill_id)`.

    Composite, not a bare timestamp. `encode_cursor` above carries a `datetime`
    alone, which is enough for the coaching feed because `created_at` is
    effectively unique there. Drills are not: a bulk import, a migration, or a
    fixture creates several inside one transaction and they share an `updated_at`
    to the microsecond. A cursor that cannot break that tie either serves a row
    twice or steps over it, and neither failure is visible in a single page.

    Base64 for the same reason as `encode_cursor`: the contract types this
    `string` and calls it opaque. Not encryption — a determined client can decode
    it; the point is that nothing about the response invites them to build one,
    because the moment they do, `updated_at` is public API.

    ## The status filter travels IN the cursor

    A cursor is a position inside one result set, and changing the filter changes
    the set. Carried across — page one under `all`, page two under `published` —
    the keyset still applies and the response still looks like a page, but every
    published drill that sorted ahead of the cursor is silently gone. The client
    is never told it lost rows.

    `_LibraryCursor` above already settled this for its own sort orders, and for
    the same stated reason: a position that is not interpretable must be refused
    rather than quietly reinterpreted.
    """

    status: CatalogStatus
    updated_at: datetime
    drill_id: UUID

    def encode(self) -> str:
        return urlsafe_b64encode(
            f"{self.status}|{self.updated_at.isoformat()}|{self.drill_id}".encode()
        ).decode()

    @classmethod
    def decode(cls, cursor: str, *, status: CatalogStatus) -> _CatalogCursor:
        """Read a cursor back, or refuse it.

        Raises:
            ProblemError: `422 validation-error` on anything this server did not
                mint, and on one it did mint under a different status filter. Both
                are the same defect from the reader's side — the position is not
                interpretable against the set they asked for — and serving a page
                anyway would hide the mistake from whoever sent it. A client cannot
                construct a valid cursor, so a bad one always came from elsewhere.
        """
        try:
            kind, at, drill = urlsafe_b64decode(cursor.encode()).decode().split("|")
            decoded = cls(
                status=cast(CatalogStatus, kind),
                updated_at=datetime.fromisoformat(at),
                drill_id=UUID(drill),
            )
        except (BinasciiError, UnicodeDecodeError, ValueError) as exc:
            raise ProblemError(
                catalog.VALIDATION_ERROR, detail="cursor is not one this server issued"
            ) from exc
        if decoded.status != status:
            raise ProblemError(
                catalog.VALIDATION_ERROR,
                detail=f"cursor belongs to status={decoded.status!r}, not {status!r}",
            )
        return decoded


_CATALOG = text(
    """
    select d.id                            as drill_id,
           d.label,
           d.status,
           d.call_type,
           d.updated_at,
           s.average_score,
           fn_score_band(s.average_score)  as band,
           coalesce(s.total_attempts, 0)   as total_attempts
    from drill d
    left join v_drill_stats s on s.drill_id = d.id
    where d.team_id = :team
      and not d.self_authored
      and (cast(:status as text) = 'all' or d.status = cast(:status as text))
      and (cast(:cursor_at as timestamptz) is null
           or (d.updated_at, d.id)
               < (cast(:cursor_at as timestamptz), cast(:cursor_id as uuid)))
    order by d.updated_at desc, d.id desc
    limit :limit
    """
)
"""The team's drills — published, draft and archived alike (FR-TRM-008).

`not d.self_authored` and `d.team_id = :team` are BOTH defence in depth here, and
saying so is the honest version. RLS already enforces each: the P3 policy
`drill_manager_team_select` calls `app_drill_manageable_by_team`, whose body is
`d.team_id = app.team_id and not d.self_authored`. Verified by deleting the
predicate and re-running — every test still passed, because the policy was doing
the work.

They stay because the query should state its own scope rather than inherit it, and
because a policy is one migration away from being widened by someone reasoning
about a different surface. But nothing here is load-bearing on its own, and a test
that goes green is not evidence that this line does anything.

The placement is still deliberate: on the DRILL rather than inside the status
filter. FR-TRP-009 makes a rep's self-authored drill private to its author
"anywhere", and a rep's drafts and published drills are both self-authored — so a
predicate written into each status branch would let each back in through its own
tab while the default view still looked correct.

`left join v_drill_stats`, not a join: V-7 excludes self-authored drills itself,
and a drill nobody has attempted still needs a row. `coalesce(total_attempts, 0)`
because a count of nothing is zero; `average_score` stays null, and the caller
refuses to band it.

## Why the rollup can be trusted here

V-7 counts graded attempts with no `not a.self_authored` predicate of its own,
which looks like a hole — a team drill's rollup silently including private
practice. It is closed by the schema rather than by SQL:
`fk_attempt_drill_id_org_id_self_authored` is composite on
`(drill_id, org_id, self_authored)`, so an attempt's flag must equal its drill's.
A drill with `not self_authored` can only carry attempts with `not self_authored`.

`order by d.updated_at desc, d.id desc` matches the cursor's `(updated_at, id)`
exactly. A keyset whose ORDER BY and predicate disagree is the classic way to skip
rows under concurrent writes, and here they are written together for that reason.
"""


async def team_catalog(
    session: AsyncSession,
    *,
    team_id: UUID,
    status: CatalogStatus,
    cursor: str | None,
    limit: int,
) -> TeamCatalog:
    """The team's drill catalog, most recently updated first (FR-TRM-008).

    Args:
        session: A scoped session; the read is RLS-filtered.
        team_id: The manager's team, from the session record.
        status: `all`, or one of the drill statuses.
        cursor: The opaque token from the previous page, or None for the first.
        limit: Page size, already bounded by the router.

    Returns:
        A page of drills and the cursor for the next, or a null cursor on the
        last.

    Raises:
        ProblemError: `422 validation-error` on a cursor this server did not mint.
    """
    # `is not None`, not truthiness. An empty `?cursor=` is a cursor this server
    # did not issue, and `decode_cursor` above already answers that with `422` —
    # treating it as "no cursor" here would make two paginators in one module
    # disagree about the same input, and silently restart the reader at the head
    # of the catalog.
    position = _CatalogCursor.decode(cursor, status=status) if cursor is not None else None
    rows = (
        await session.execute(
            _CATALOG,
            {
                "team": team_id,
                "status": status,
                "cursor_at": position.updated_at if position else None,
                "cursor_id": position.drill_id if position else None,
                # One more than asked for: its presence IS `has_more`, which
                # avoids a second count query that could disagree with the page.
                "limit": limit + 1,
            },
        )
    ).all()

    has_more = len(rows) > limit
    page = rows[:limit]

    return TeamCatalog(
        data=[
            CatalogItem(
                drill_id=row.drill_id,
                label=row.label,
                status=row.status,
                call_type=row.call_type,
                average_score=_score_band(row.average_score, row.band),
                total_attempts=int(row.total_attempts),
                updated_at=row.updated_at,
            )
            for row in page
        ],
        pagination=CursorPage(
            next_cursor=(
                _CatalogCursor(status, page[-1].updated_at, page[-1].drill_id).encode()
                if page and has_more
                else None
            ),
            has_more=has_more,
        ),
    )
