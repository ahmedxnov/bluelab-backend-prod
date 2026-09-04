"""AC-TRM-001 and AC-TRM-002: the rating stack against hand-computation.

The criterion is not "the views return something plausible" — it is that every
rating, the team average, the tier counts and the gap rows **match
hand-computation from FR-TRM-002/003/004**. So every expected value below is a
literal, worked out from the seeded scores by hand and written down, not derived
in the test from the same arithmetic the code uses. A test that recomputed the
mean in Python would agree with a broken view for the same reason the view was
broken.

Five reps, because FR-TRM-003 suppresses tiering entirely below five and the
whole stack returns null. At five the arithmetic is checkable and non-trivial:
`greatest(1, round(5 × 0.2))` is 1, so exactly one top performer and one
needs-coaching, three mid, and the bottom half is `rank_desc > ceil(5/2.0)` —
ranks 4 and 5.

## What must be counted nowhere (FR-TRM-002)

Three exclusions, seeded so that a dropped predicate moves a number rather than
going quiet:

**Self-authored practice** (FR-TRP-010) sits on the TOP performer, scoring 2.0.
If it ever entered the counted pool that rep's rating falls from 8.1 to 6.6, they
drop from rank 1 to rank 4, the tier assignments scramble, the team average
moves, and every gap row changes — because the top cohort is that one rep. It is
the loudest placement available.

**Non-graded attempts** (FR-SCR-014) are seeded as an interrupted attempt with no
scorecard, which V-1 excludes twice over: on `status = 'graded'` and on the inner
join to `scorecard`.

**Test calls** are seeded as nothing at all, and that is not an omission.
FR-DRL-012 says a test call "leaves no attempt record, no review, and no trace in
any statistic", and the schema carries no flag for one — there is no `is_test`
column on `attempt` and no row shape a test call could take. The exclusion is
structural rather than a predicate, so there is nothing to seed and nothing a
regression could re-admit. AC-TRM-001 names a test call in its Given; this is
where that lands, and pretending otherwise by inventing a row would be testing a
fiction.

## Scope of these tests

They read the views through the migration role, so RLS is not in force. That is
deliberate and it bounds the claim: this is the arithmetic, not the scoping.
Whether a manager may see these numbers is the team surface's question and is
answered where that surface is tested.

## Why nothing here is tagged with an FR

These tests were first tagged `FR-TRM-002/003/004` and `FR-SCR-014`, and the
coverage ratchet went red for closing four baseline gaps. The audit it forced
found all four claims partial, so the tags came off rather than the baseline
being updated:

- **FR-SCR-014** is the attempt RECORD — drill, participant, org, start and end
  times, duration, the status transitions, the restart flag. Nothing here asserts
  any of it. FR-TRM-002 merely cites it for the phrase "non-graded attempts", and
  citing is not verifying.
- **FR-TRM-002** also requires the per-call-type rating and the trend against the
  prior month. This fixture seeds one month, so trend cannot be observed at all.
- **FR-TRM-003** also requires tiering **suppressed below five rated reps**. Five
  is the only size seeded here, and a one-versus-five pair would not pin the
  boundary either — that needs a four-rep case.
- **FR-TRM-004** says the **dashboard** shall present these, with a delta against
  the prior month and severity marks. No dashboard exists, and severity is not
  computed by V-6 at all; it belongs to the response.

Each becomes claimable when the missing half is built. Tagging them now would
have bought four covered requirements in a report and cost the report its
meaning.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

pytestmark = [pytest.mark.l3_integration]

# Expected values are `Decimal`, not float. `numeric` arrives as `Decimal`, and
# `Decimal("2.3") == 2.3` is False — the float literal is 2.2999999999999998. The
# alternative is to convert every reading to float first, which would put a lossy
# step inside the one test whose entire subject is exactness. So the oracle is
# written in the type the database actually returns.

_LOCAL_MONTH = "date_trunc('month', now() at time zone 'UTC')"


async def _rows(engine: AsyncEngine, sql: str, **params: object) -> Sequence[Row[Any]]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        return (await session.execute(text(sql), params)).all()


# ── The premise ───────────────────────────────────────────────────────────────


@pytest.mark.verifies("AC-TRM-001")
async def test_the_excluded_rows_are_actually_there(training_engine, team_world):
    """Guards every exclusion assertion below against passing vacuously.

    "The self-authored attempt is counted nowhere" is satisfied just as well by an
    insert that silently failed, or by a row that landed in the previous month
    because the fixture anchored on `now() - N days` near a boundary. Then the
    interesting tests go green having proved nothing, and stay green through the
    exact regression they exist to catch.

    So this asserts the shape of what was seeded, in the month the others read:
    fifteen counted attempts, one graded self-authored, one interrupted.
    """
    rows = await _rows(
        training_engine,
        "select a.self_authored, a.status, count(*) as n from attempt a"
        # `at time zone 'UTC'` is not decoration. Without it this truncates in the
        # SESSION zone, which is `Africa/Cairo` here — so in the last three hours
        # of any month the session has rolled over and UTC has not, this reads the
        # next month, matches nothing, and reports zero rows. The seven tests below
        # would keep passing, because they read `local_month` off the views, which
        # buckets in the org's zone. Verified against a fixed boundary timestamp.
        f" where a.team_id = :team"
        f" and date_trunc('month', a.started_at at time zone 'UTC') = {_LOCAL_MONTH}"
        " group by 1, 2 order by 1, 2",
        team=team_world.team,
    )

    assert [(r.self_authored, r.status, r.n) for r in rows] == [
        # 16, not 15: five reps × three call types, plus one upsell attempt held
        # by a single mid rep so that a null-gap row exists to sort.
        (False, "graded", 16),
        (False, "interrupted", 1),
        (True, "graded", 1),
    ]

    # Every attempt carries a distinct `started_at`. No aggregate in this file can
    # observe that, so it is asserted here rather than discovered later: the first
    # test to order a rep's attempts — the deep dive's recent list — would
    # otherwise be non-deterministic, and only on runs where the timestamps happened
    # to collide.
    distinct = await _rows(
        training_engine,
        "select count(*) as n, count(distinct started_at) as distinct_starts"
        " from attempt where team_id = :team",
        team=team_world.team,
    )
    # 22 across every month, not the 17 counted above: the fixture also seeds one
    # prior-month attempt per rep, so the dashboard's delta has a month to
    # subtract. They fall outside the month filter above and inside this one,
    # which is the distinction worth keeping visible.
    assert distinct[0].n == distinct[0].distinct_starts == 22


# ── V-2: the monthly rating (FR-TRM-002) ──────────────────────────────────────


@pytest.mark.verifies("AC-TRM-001")
async def test_every_rep_rating_matches_hand_computation(training_engine, team_world):
    """Five means, worked by hand from the seeded scores.

    Rep 1 is `(9.0 + 8.2 + 7.0) / 3 = 8.0667`, which rounds to 8.1 — a case where
    the rounding actually bites, so a view that truncated instead would fail here
    rather than agreeing by luck.
    """
    rows = await _rows(
        training_engine,
        f"select rep_account_id, rating, counted_attempts from v_rep_monthly_rating"
        f" where team_id = :team and month = {_LOCAL_MONTH} order by rating desc",
        team=team_world.team,
    )

    assert [(r.rating, r.counted_attempts) for r in rows] == [
        (Decimal("8.1"), 3),
        (Decimal("7.6"), 3),
        # Four, not three: this rep also holds the lone upsell attempt. Its 7.0
        # moves their mean from 7.0333 to 7.025, which still rounds to 7.0 — so
        # the rating and the rank below are unchanged by it.
        (Decimal("7.0"), 4),
        (Decimal("6.7"), 3),
        (Decimal("6.5"), 3),
    ]
    assert [r.rep_account_id for r in rows] == list(team_world.reps)


@pytest.mark.verifies("AC-TRM-001")
async def test_the_top_performers_private_practice_is_counted_nowhere(
    training_engine, team_world
):
    """The predicate the whole stack leans on (FR-TRP-010, AC-TRP-004).

    Rep 1 holds a graded self-authored attempt scoring 2.0 this month. Counted, it
    would drag their rating to 6.6 and cost them the top tier. `counted_attempts`
    is asserted alongside the rating because a leak has to move both, and a test
    that watched only the mean could be satisfied by a compensating error.
    """
    rows = await _rows(
        training_engine,
        f"select rating, counted_attempts from v_rep_monthly_rating"
        f" where rep_account_id = :rep and month = {_LOCAL_MONTH}",
        rep=team_world.reps[0],
    )

    assert [(r.rating, r.counted_attempts) for r in rows] == [(Decimal("8.1"), 3)]


@pytest.mark.verifies("AC-TRM-001")
async def test_a_non_graded_attempt_is_counted_nowhere(training_engine, team_world):
    """Rep 2 holds an interrupted attempt with no scorecard.

    Their three counted attempts are unchanged by it. V-1 excludes it on
    `status = 'graded'` and again on the inner join to `scorecard`, so this
    survives either predicate being dropped alone — which is the point of
    asserting the count rather than the rating.
    """
    rows = await _rows(
        training_engine,
        f"select counted_attempts from v_rep_monthly_rating"
        f" where rep_account_id = :rep and month = {_LOCAL_MONTH}",
        rep=team_world.reps[1],
    )

    assert [r.counted_attempts for r in rows] == [3]


# ── V-4: performance tiers (FR-TRM-003) ───────────────────────────────────────


@pytest.mark.verifies("AC-TRM-001")
async def test_the_tiers_split_one_top_three_mid_one_needs_coaching(
    training_engine, team_world
):
    """`greatest(1, round(5 × 0.2))` is 1 at each end, leaving three in the middle.

    The 20% is asserted at the size where rounding decides it: 5 reps rounds to
    exactly 1, so an implementation using floor, ceil or a bare 20% would all
    agree here — which is why the ranks are asserted too. A tier set that was
    right by count but attached to the wrong reps fails on the second assertion.
    """
    rows = await _rows(
        training_engine,
        f"select rep_account_id, tier, rank_desc, rated_reps from v_rep_month_tier"
        f" where team_id = :team and month = {_LOCAL_MONTH} order by rank_desc",
        team=team_world.team,
    )

    assert [r.tier for r in rows] == ["top", "mid", "mid", "mid", "needs_coaching"]
    assert [r.rank_desc for r in rows] == [1, 2, 3, 4, 5]
    assert {r.rated_reps for r in rows} == {5}
    assert [r.rep_account_id for r in rows] == list(team_world.reps)


# ── V-5: the team month (FR-TRM-004) ──────────────────────────────────────────


@pytest.mark.verifies("AC-TRM-001")
async def test_the_team_average_is_the_mean_of_member_ratings(training_engine, team_world):
    """`(8.1 + 7.6 + 7.0 + 6.7 + 6.5) / 5 = 7.18`, which rounds to 7.2.

    The mean of the ROUNDED member ratings, not of the raw attempt scores — those
    differ here (the scores mean 7.1867), so a view that averaged attempts
    directly would report 7.2 as well only by coincidence of this data. The tier
    counts are asserted in the same row because they come from the same view and
    a reader should see them agree.
    """
    rows = await _rows(
        training_engine,
        f"select team_average, top_performers, needs_coaching, rated_reps"
        f" from v_team_month where team_id = :team and month = {_LOCAL_MONTH}",
        team=team_world.team,
    )

    assert len(rows) == 1
    row = rows[0]
    assert row.team_average == Decimal("7.2")
    assert (row.top_performers, row.needs_coaching, row.rated_reps) == (1, 1, 5)


# ── V-6: gap analysis (FR-TRM-004, AC-TRM-002) ────────────────────────────────


@pytest.mark.verifies("AC-TRM-002")
async def test_gap_analysis_orders_by_pain(training_engine, team_world):
    """The criterion's own worked example: gaps of 2.3, 1.4 and 0.6.

    Top cohort is rep 1 alone; the bottom half is `rank_desc > ceil(5/2.0)`, so
    reps 4 and 5. Per call type that gives 9.0 vs (6.8+6.6)/2, 8.2 vs
    (6.9+6.7)/2, and 7.0 vs (6.5+6.3)/2 — 2.3, 1.4 and 0.6, which AC-TRM-002
    marks high, moderate and unmarked.

    Severity itself is not asserted here because the views do not compute it: V-6
    returns the gap and the thresholds live in the response. This pins the
    numbers the banding will be applied to, and the ordering that must follow.

    ## The upsell row leads, and that is the point

    Ordered by a plain `gap desc`, the NULL-gap row comes FIRST — PostgreSQL sorts
    NULLs first under `desc`. Upsell was attempted by one mid rep, so neither
    cohort has an average and the gap is null.

    That is asserted here deliberately, as the premise for what the response layer
    does about it: `service._GAP_ANALYSIS` adds `nulls last`, and without this
    case nothing would show why. A "worst first" list headed by the one row
    carrying no finding is the defect that clause exists to prevent.
    """
    rows = await _rows(
        training_engine,
        f"select call_type, top_avg, bottom_half_avg, gap from v_gap_analysis"
        f" where team_id = :team and month = {_LOCAL_MONTH} order by gap desc",
        team=team_world.team,
    )

    assert [(r.call_type, r.gap) for r in rows] == [
        ("upsell", None),  # NULLs first under `desc` — the default this repo overrides
        ("discovery", Decimal("2.3")),
        ("post_proposal", Decimal("1.4")),
        ("renewal", Decimal("0.6")),
    ]
    assert [(r.top_avg, r.bottom_half_avg) for r in rows] == [
        (None, None),
        (Decimal("9.0"), Decimal("6.7")),
        (Decimal("8.2"), Decimal("6.8")),
        (Decimal("7.0"), Decimal("6.4")),
    ]


@pytest.mark.verifies("AC-TRM-001")
async def test_the_manager_holds_no_rating_of_their_own(training_engine, team_world):
    """A manager with no attempts must not appear among the rated.

    `rated_reps` of 5 above already implies it, but only while the manager happens
    to have no attempts. Asserting the absence directly means a future fixture
    that gives the manager one — or a view that started counting them — fails here
    with a clear reason rather than shifting the team average by an unexplained
    amount somewhere else.
    """
    rows = await _rows(
        training_engine,
        f"select 1 from v_rep_monthly_rating"
        f" where rep_account_id = :manager and month = {_LOCAL_MONTH}",
        manager=team_world.manager,
    )

    assert rows == []
