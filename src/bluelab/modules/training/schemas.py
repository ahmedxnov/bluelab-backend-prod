"""Pydantic request and response models for this module's slice of the contract.

The contract is `api/openapi.yaml` and it is authoritative (ADR-0035): these
models are checked against it by the conformance diff, they do not define it.
`snake_case` fields, singular per glossary (api/00 §2).

## Every score travels with its band

`ScoreBand` is one object, never a bare number. FR-SCR-005 puts the banding rule
server-side — green ≥ 7.5, amber ≥ 5.5, red below — precisely so that no client
re-derives it. Two clients rounding differently is two products, and the one that
matters is the one the rep is looking at when deciding whether they are doing
well. Shipping the score alone would invite exactly that.

## Nullable is a state, not an omission

`rating`, `delta`, `direction` and every score are `| None` because "no counted
attempts this month" is a real and common answer, especially for a new rep. The
contract types them nullable rather than absent so a client renders an empty
state instead of branching on a missing key (FR-TRP-001).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

Band = Literal["green", "amber", "red"]
"""Server-computed tier (FR-SCR-005). Clients render; they never re-derive."""

CallType = Literal["discovery", "post_proposal", "renewal", "upsell"]
"""The four call types (FR-DRL-001)."""

LeadType = Literal["inbound_quote", "referral", "cold_outreach"]
"""Discovery-only (FR-DRL-001). The schema enforces the iff — a discovery drill
carries one and no other call type may — so this is never populated by choice
here; it is read back off the drill."""

LibrarySource = Literal["assigned", "self_authored", "library"]
"""Where a card came from, and it is single-valued by contract even though a drill
can be both assigned and part of the team library.

`assigned` wins that overlap: it is the only source carrying a due date and a
lock, so demoting it to `library` would drop the two things the rep has to act on.
The other pair cannot collide — reps author without an assignment step
(FR-TRP-009), so `assigned` and `self_authored` are disjoint by construction."""

SourceFilter = Literal["all", "assigned", "self_authored"]
"""The three tabs (FR-TRP-007). Note there is no `library` tab: the team's drills
are visible under All and nowhere else, which is the contract's enum, not an
omission."""

AttemptedFilter = Literal["all", "attempted", "unattempted"]

LibrarySort = Literal["recommended", "recently_assigned"]

Direction = Literal["rising", "slipping", "flat"]

MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
"""Calendar month in the ORG's timezone, not the server's — `v_counted_attempt`
buckets on `started_at at time zone o.timezone`, so a rep in Cairo and the process
in Frankfurt agree on which month an attempt belongs to."""

Month = Annotated[str, Field(pattern=MONTH_PATTERN)]


class _Request(BaseModel):
    """Strict by default.

    `extra="forbid"`, for the reason the identity requests give: an unknown member
    is a client that believes it is sending something this server will honour, and
    dropping it silently is how a control gets enabled client-side and ignored
    server-side (api/04 §3).
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ScoreBand(BaseModel):
    """A score and the band it falls in, always together (FR-SCR-005)."""

    score: Annotated[float, Field(ge=0, le=10)]
    band: Band


class CursorPage(BaseModel):
    """Opaque cursor pagination. `next_cursor` is null on the last page."""

    next_cursor: str | None = None
    has_more: bool


# ── GET /me/progress ──────────────────────────────────────────────────────────


class WeeklyPoint(BaseModel):
    week_start: date
    score: float | None = None


class CallTypeCard(BaseModel):
    """One card per call type over the trailing 30 days (FR-TRP-001)."""

    call_type: CallType
    rating: ScoreBand | None = None
    attempts: Annotated[int, Field(ge=0)]
    weakest: bool
    """Tagged server-side, and the card list is ordered weakest first. The client
    shows the prompt; it does not pick which call type the rep should work on."""


class ProgressHome(BaseModel):
    month: Month
    rating: ScoreBand | None = None
    """Null with no counted attempts this month (FR-TRP-001)."""
    delta: float | None = None
    """Change against the prior month."""
    direction: Direction | None = None
    weekly_trend: list[WeeklyPoint]
    call_type_cards: list[CallTypeCard]


# ── GET /me/profile ───────────────────────────────────────────────────────────


class BadgeView(BaseModel):
    """Earned badges carry the winning rule; locked ones carry the goal.

    Both are returned so the profile shows progress rather than only achievement.
    Owner-only (FR-TRP-005), which is why this lives on `/me` and has no
    team-facing counterpart.
    """

    code: str
    name: str
    rule_text: str
    earned: bool
    earned_at: datetime | None = None


class ProfileView(BaseModel):
    account_id: UUID
    display_name: str
    email: EmailStr
    total_completed_drills: Annotated[int, Field(ge=0)]
    """Lifetime graded attempts (FR-TRP-004)."""
    lifetime_rating: ScoreBand | None = None
    badges: list[BadgeView]


# ── GET /me/coach-feedback · POST /me/coach-feedback/mark-read ────────────────


class CoachFeedbackItemView(BaseModel):
    id: UUID
    body: str
    created_at: datetime
    read: bool


class CoachFeedbackPage(BaseModel):
    data: list[CoachFeedbackItemView]
    unread_count: Annotated[int, Field(ge=0)]
    pagination: CursorPage


class MarkReadRequest(_Request):
    through_id: UUID
    """Marks this item and every older one read.

    Through-an-id rather than a list of ids: the client marks what the reader has
    scrolled past, and a list would make the outcome depend on which items
    happened to be in the page it was holding.
    """


class MarkReadResult(BaseModel):
    unread_count: Annotated[int, Field(ge=0)]


# ── GET /me/library ───────────────────────────────────────────────────────────


class LibraryAssignment(BaseModel):
    """Present only on assigned cards (FR-TRP-006/013).

    All four fields are required together because they are read together: the card
    renders "{used} of {allowed} used" and greys out on `locked`. A partial object
    would let a client show a due date it cannot say anything about.
    """

    due_date: date
    """The ORG's calendar date, not the server's. "Due Friday" has to mean Friday
    where the rep works (data/01 §6)."""
    attempts_used: Annotated[int, Field(ge=0)]
    attempts_allowed: Annotated[int, Field(ge=1)]
    locked: bool
    """Allowance exhausted — no call can start until the manager re-assigns
    (FR-TRP-013). Server-computed from used vs allowed rather than left to the
    client, for the same reason bands are: two clients comparing differently is
    two products."""


class LibraryCard(BaseModel):
    """One practicable drill — or one of the caller's own drafts (FR-DRL-013).

    Drafts appear in `self_authored` and nowhere else, marked `status="draft"`,
    and they are not startable. They carry a null `label` because the persona that
    names a drill is generated at publish (FR-DRL-004), so an unpublished one has
    no name to show yet.
    """

    drill_id: UUID
    status: Literal["draft", "published"]
    label: str | None = None
    call_type: CallType
    lead_type: LeadType | None = None
    source: LibrarySource
    attempted: bool
    best: ScoreBand | None = None
    """The rep's own best, or null when unattempted. Never a teammate's — the
    query filters by account explicitly, because the view it reads would show a
    manager their whole team."""
    assignment: LibraryAssignment | None = None


class LibraryCounts(BaseModel):
    """Live per-tab counts (FR-TRP-007).

    Computed over the set the call-type and attempt filters produced, but BEFORE
    the source filter — so the counts describe what each tab would show if you
    clicked it, rather than what the current tab holds. A window function in the
    final select computes the wrong thing here, because `WHERE` runs first.
    """

    all: Annotated[int, Field(ge=0)]
    assigned: Annotated[int, Field(ge=0)]
    self_authored: Annotated[int, Field(ge=0)]


class LibraryPage(BaseModel):
    data: list[LibraryCard]
    counts: LibraryCounts
    pagination: CursorPage


# ── GET /drills/{drill_id}/my-history ─────────────────────────────────────────

AttemptStatus = Literal[
    "in_progress", "completed", "grading_pending", "graded", "interrupted"
]
"""Every state an attempt can be listed in. `interrupted` leaves the row and
stores no capture (FR-LIV-015), so it appears in the history unscored rather than
vanishing — the rep took it, and a list that hid it would not add up."""


class DrillStats(BaseModel):
    """The statistics primitives (FR-SCR-015), derived from GRADED attempts only.

    **Bare numbers, not `ScoreBand`** — and that is the contract's choice, not an
    oversight. A band is what a client renders on a score it displays as a score;
    these four are inputs to a trend line and a headline figure that the history
    screen composes itself. `attempts[].score` below IS banded, because each of
    those is rendered as a score.

    All four are nullable together: a drill with no graded attempts has no best
    and no trend, and that is an absence rather than a zero.
    """

    best: float | None = None
    latest: float | None = None
    average: float | None = None
    trend: float | None = None
    """Latest versus FIRST (FR-SCR-015) — so a single graded attempt trends 0.0,
    not null. Zero is "measured, and flat"; null is "not measured", and a rep who
    has attempted once is in the first state."""
    graded_attempts: Annotated[int, Field(ge=0)]


class AttemptSummary(BaseModel):
    """One row of the attempt list, newest first (FR-TRP-011)."""

    attempt_id: UUID
    """What the review opens (FR-SCR-010). The review is a separate operation;
    what this endpoint owes it is a stable id per row."""
    attempt_number: Annotated[int, Field(ge=1)]
    """Derived, not stored — `attempt` has no such column. Counts from the OLDEST
    attempt, while the list runs newest first, so the two orderings deliberately
    disagree: FR-TRP-012's chart reverses the list and reads these as its x-axis."""
    started_at: datetime
    status: AttemptStatus
    score: ScoreBand | None = None
    """Null until graded. An in-progress or interrupted attempt is a real row with
    no score, not a missing row."""


class DrillHistory(BaseModel):
    drill_id: UUID
    stats: DrillStats
    attempts: list[AttemptSummary]


# ── GET /team/dashboard ───────────────────────────────────────────────────────


TIERING_MINIMUM = 5
"""Below this many rated reps, tiering is suppressed (FR-TRM-003).

Named here rather than written as a bare `5` in the service, because V-4 encodes
the same threshold in SQL (`case when rated_reps < 5 then null`). Two copies of a
rule is one edit away from disagreeing, and this is the one a reader of the
response can see."""

Severity = Literal["high", "moderate", "none"]
"""How badly a call type's gap hurts (FR-TRM-004): ≥ 2.0 high, ≥ 1.0 moderate.

Server-computed, like every band on this surface. V-6 returns the gap and stops
there, so these thresholds exist in exactly one place — and `none` is a value
rather than an absent field, so a client renders three states without branching
on a missing key."""


class GapRow(BaseModel):
    """One call type's top-versus-bottom-half comparison (FR-TRM-004).

    Both cohort averages travel with the gap. A gap of 2.3 between 9.0 and 6.7 is
    a different coaching conversation from the same gap between 5.0 and 2.7, and
    the difference alone cannot distinguish them.
    """

    call_type: CallType
    top_avg: float | None
    bottom_half_avg: float | None
    gap: float | None
    severity: Severity


class TeamDashboard(BaseModel):
    """The month's coaching picture (FR-TRM-004).

    Every field is present on an empty month rather than omitted: a team with no
    counted attempts is ordinary — a new team, or the first day of a month — and
    the client renders an empty state rather than branching on missing keys.
    """

    month: Month
    team_average: ScoreBand | None
    """Null when nobody was rated. NOT a red zero: `fn_score_band(null)` returns
    `'red'` because `null >= 7.5` is null and falls through to the else, so an
    unguarded read reports a quiet month as an alarming one."""

    delta: float | None
    """Against the prior month. Null rather than zero when there is no prior
    month — zero is a finding, and a team measured once has not held steady."""

    rated_reps: int = Field(ge=0)
    tiering_suppressed: bool
    """True below five rated reps (FR-TRM-003), where the 20% split stops carrying
    meaning. Distinct from "no top performers": the counts below are zero in both
    cases and only this flag says which."""

    top_performers: int = Field(ge=0)
    needs_coaching: int = Field(ge=0)
    gap_analysis: list[GapRow]
    """Widest gap first. Only call types with counted attempts in BOTH cohorts
    appear — unlike the rep's call-type cards, which return all four so an untried
    type cannot hide. A gap needs two sides to exist, and a row of nulls would be
    noise on a surface whose job is to rank where it hurts."""


# ── GET /team/roster ──────────────────────────────────────────────────────────


Tier = Literal["top", "mid", "needs_coaching"]
"""V-4's assignment (FR-TRM-003). Null on the wire while tiering is suppressed."""


class RosterRow(BaseModel):
    """One rep's month (FR-TRM-005).

    Every field except the identity is nullable, because "on the team but not
    measured this month" is an ordinary state and the roster names every rep
    regardless — a new joiner, or someone who took no calls. Null is not zero:
    zero would say they were measured and scored nothing.
    """

    account_id: UUID
    display_name: str
    tier: Tier | None
    """Null while tiering is suppressed, and null for an unrated rep. The two are
    distinguishable from the dashboard's `tiering_suppressed`, not from here."""

    rating: ScoreBand | None
    counted_attempts: int = Field(ge=0)
    """A count, so zero rather than null when there is nothing to count."""

    strongest_call_type: CallType | None = None
    weakest_call_type: CallType | None = None
    """Both null with nothing measured, and both the SAME call type when only one
    was — which is honest rather than tidy: that one type is simultaneously their
    best and their worst, and hiding it would hide that it is their only one."""


# ── GET /team/drills ──────────────────────────────────────────────────────────


DrillStatus = Literal["draft", "published", "archived"]

CatalogStatus = Literal["all", "draft", "published", "archived"]
"""The catalog's filter, which carries an `all` the drill's own status cannot."""


class CatalogItem(BaseModel):
    """One of the team's drills, with its rollup (FR-TRM-008)."""

    drill_id: UUID
    label: str | None
    """Null for a draft with no generated scenario yet. Not an empty string — that
    would claim it has a name and the name is blank."""

    status: DrillStatus
    call_type: CallType
    average_score: ScoreBand | None
    """Null when nobody has attempted it. NOT a banded zero: `fn_score_band(null)`
    returns `'red'`, so an unguarded read shows an untried drill as a bad one."""

    total_attempts: int = Field(ge=0)
    updated_at: datetime


class TeamCatalog(BaseModel):
    data: list[CatalogItem]
    pagination: CursorPage
    """Keyed on `(updated_at, drill_id)`, not on the timestamp alone. Drills
    created in one transaction — a bulk import, or a fixture — share an
    `updated_at` to the microsecond, and a cursor that cannot break that tie either
    repeats rows or skips them."""


# ── GET /team/drills/{drill_id}/stats · GET /team/cohorts ────────────────────


class DrillRollup(BaseModel):
    """The published drill's V-7 aggregate (FR-SCR-016)."""

    team_average: ScoreBand | None = None
    reps_practiced: Annotated[int, Field(ge=0)]
    eligible_reps: Annotated[int, Field(ge=0)]
    total_attempts: Annotated[int, Field(ge=0)]


class DrillLeaderboardRow(BaseModel):
    """One participant's V-8 best with the most-recent replay entry point."""

    account_id: UUID
    display_name: str
    best: ScoreBand
    last_attempt_at: datetime
    latest_attempt_id: UUID


class TeamDrillStats(BaseModel):
    """Rollup and best-score leaderboard for one published team drill."""

    drill_id: UUID
    rollup: DrillRollup
    leaderboard: list[DrillLeaderboardRow]


class CohortsView(BaseModel):
    """Deterministically ordered, active-rep assignment quick picks (FR-TRM-014)."""

    month: Month
    bottom_half: list[UUID]
    rating_below_6: list[UUID]
    fewer_than_5_attempts: list[UUID]
    newest_joiners: list[UUID]


# ── GET /team/reps/{account_id} ───────────────────────────────────────────────


class AssignmentPut(BaseModel):
    """The complete desired assignment state (FR-TRM-011/012/013)."""

    recipient_account_ids: Annotated[list[UUID], Field(min_length=1)]
    due_date: date
    attempts_allowed: Annotated[int, Field(ge=1)]


class AssignmentRecipientView(BaseModel):
    """One fresh recipient allowance returned after an assignment write."""

    account_id: UUID
    attempts_used: Annotated[int, Field(ge=0)]
    granted_at: datetime


class AssignmentView(BaseModel):
    """The persisted, single-assignment state for a drill."""

    drill_id: UUID
    due_date: date
    attempts_allowed: Annotated[int, Field(ge=1)]
    recipients: list[AssignmentRecipientView]
    updated_at: datetime


class CallTypeProfile(BaseModel):
    """One call type's standing for a rep this month (FR-TRM-006)."""

    call_type: CallType
    rating: ScoreBand | None
    """Null when untried. Distinct from a low score, which is a measurement."""

    counted_attempts: int = Field(ge=0)
    """Zero when untried — a count, and zero is the true one."""


class RecentAttempt(BaseModel):
    """A counted attempt, and the way back into its replay (FR-TRM-007)."""

    attempt_id: UUID
    """The replay entry point — `GET /attempts/{id}/review`."""

    drill_label: str
    call_type: CallType
    started_at: datetime
    score: ScoreBand
    """Never null here, unlike the rep's own history. These are COUNTED attempts,
    and counted means graded — an in-progress or interrupted attempt is not in
    this list at all."""

    attempt_number: int = Field(ge=1)
    """Its place in that DRILL's history, counted from the oldest — not its
    position in this list, and not its place within the month. FR-TRM-007 puts
    this number in the replay chrome, and FR-TRP-002 requires the rep to read the
    same one for the same attempt."""


class RepDeepDive(BaseModel):
    """One rep's month, in the manager's framing (FR-TRM-006).

    The same pool and the same formulas as the rep's own surface (FR-TRP-002) —
    "the two surfaces render the same numbers, reframed". Anything computed here
    rather than read from the derivations would be a second definition.
    """

    account_id: UUID
    display_name: str
    month: Month
    rating: ScoreBand | None
    trend: float | None
    """Against the prior month. Null rather than zero with no prior month: zero
    says they held steady, null says they have not been measured twice."""

    per_call_type: list[CallTypeProfile]
    """All four, always — unlike the dashboard's gap rows, which omit a call type
    that cannot be compared. A profile is a picture of the whole rep, and an
    omitted row would hide exactly the call type they have been avoiding."""

    recent_attempts: list[RecentAttempt]
    """This month's counted attempts, newest first."""


class TeamRoster(BaseModel):
    month: Month
    data: list[RosterRow]
    """Every active rep on the team, rating descending, unrated last. The contract
    wraps the list in an object rather than returning a bare array so the month can
    travel with it — the client renders "August" above the table without a second
    call."""
