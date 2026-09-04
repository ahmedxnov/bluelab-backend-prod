"""`GET /team/dashboard` — the month's coaching picture (FR-TRM-004).

The arithmetic behind these numbers is pinned against hand-computation in
`test_trm_formulas.py`, which reads the views directly. This file is about what
the *endpoint* does with them: that a manager reaches it and a rep does not, that
every score arrives with its band, that severity and ordering are applied where
the views deliberately stop, and that an empty month renders as an empty month
rather than as an error or a hole in the response.

So the expected values are the same literals as the formula tests — deliberately.
If the two ever disagree, one of them is describing a surface the other is not,
and that is exactly the drift worth failing on.

Both worlds are seeded here: `client` pulls in `world` for the app, and
`team_world` provides the five rated reps. They are separate orgs, so nothing
crosses; that RLS keeps them apart is asserted where isolation is the subject,
not re-litigated here.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.l3_integration]

DASHBOARD = "/api/v1/team/dashboard"




# ── The headline numbers ──────────────────────────────────────────────────────


@pytest.mark.verifies("FR-TRM-004")
async def test_the_dashboard_reports_the_months_coaching_picture(
    client, sign_in, team_world, org_month
):
    """Every headline value, against the same literals the formula tests use.

    `7.2` bands amber because the cut is 7.5. That the band travels WITH the score
    rather than beside it is FR-SCR-005 — no client re-derives it.
    """
    await sign_in(team_world.manager_email)

    response = await client.get(DASHBOARD)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["month"] == await org_month()
    assert body["team_average"] == {"score": 7.2, "band": "amber"}
    assert body["rated_reps"] == 5
    assert body["tiering_suppressed"] is False
    assert body["top_performers"] == 1
    assert body["needs_coaching"] == 1


@pytest.mark.verifies("FR-TRM-004")
async def test_the_delta_is_the_change_from_the_prior_month(client, sign_in, team_world):
    """`7.2 - 6.0 = 1.2`, against a prior month that actually exists.

    The clause this covers is the one an all-null delta cannot: the service joins
    `prev.month = cur.month - interval '1 month'`, and a join written `+` instead
    of `-`, or onto the wrong column, returns null in every case where no prior
    month is seeded. Every other assertion in this file would still pass.

    The prior month is flat at 6.0 for all five reps, so the expected value comes
    from one subtraction rather than from re-deriving the fixture.
    """
    await sign_in(team_world.manager_email)

    body = (await client.get(DASHBOARD)).json()

    assert body["delta"] == 1.2


@pytest.mark.verifies("FR-TRM-004")
async def test_the_delta_is_null_when_there_is_no_prior_month(
    client, sign_in, team_world, org_month
):
    """A first measured month has no delta — null, not zero.

    Zero is a finding: it says the team held steady. Null says they have not been
    measured twice, which is the true statement about a team whose first month
    this is.

    Asked of the PRIOR month, which is the only month here that is itself
    preceded by nothing — so the row exists and carries a team average, and only
    the delta is absent. Asking an empty month instead would prove far less: the
    delta is null there because the whole row is.
    """
    await sign_in(team_world.manager_email)
    prior = await org_month(months_back=1)

    body = (await client.get(DASHBOARD, params={"month": prior})).json()

    assert body["month"] == prior
    assert body["team_average"] == {"score": 6.0, "band": "amber"}
    assert body["delta"] is None


# ── Gap analysis (AC-TRM-002) ─────────────────────────────────────────────────


@pytest.mark.verifies("AC-TRM-002")
async def test_gap_rows_are_ordered_widest_first_and_marked(client, sign_in, team_world):
    """The criterion's worked example: 2.3, 1.4, 0.6 → high, moderate, unmarked.

    Severity is applied here rather than in the view, because V-6 returns the gap
    and nothing else. That makes this the only place the thresholds exist, and the
    only place a `>=` slipping to `>` would show.
    """
    await sign_in(team_world.manager_email)

    rows = (await client.get(DASHBOARD)).json()["gap_analysis"]

    assert [(r["call_type"], r["gap"], r["severity"]) for r in rows] == [
        ("discovery", 2.3, "high"),
        ("post_proposal", 1.4, "moderate"),
        ("renewal", 0.6, "none"),
        ("upsell", None, "none"),
    ]


@pytest.mark.verifies("FR-TRM-004")
async def test_each_gap_row_carries_both_cohort_averages(client, sign_in, team_world):
    """The gap alone does not tell a coach where to aim.

    A gap of 2.3 between 9.0 and 6.7 is a different conversation from one between
    5.0 and 2.7, and the contract requires both sides for that reason.
    """
    await sign_in(team_world.manager_email)

    rows = (await client.get(DASHBOARD)).json()["gap_analysis"]

    assert [(r["top_avg"], r["bottom_half_avg"]) for r in rows] == [
        (9.0, 6.7),
        (8.2, 6.8),
        (7.0, 6.4),
        (None, None),
    ]


@pytest.mark.verifies("AC-TRM-002")
async def test_a_gap_that_cannot_be_computed_sorts_last(client, sign_in, team_world):
    """The one row carrying no finding must not head a worst-first list.

    Upsell was attempted by a single MID rep — in neither cohort — so both cohort
    averages are null and so is the gap. Ordered by a plain `gap desc` this row
    comes FIRST, because PostgreSQL sorts NULLs first under `desc`;
    `test_trm_formulas.py::test_gap_analysis_orders_by_pain` asserts exactly that
    against the raw view, which is the premise for this.

    `_GAP_ANALYSIS` adds `nulls last` to correct it. Deleting that clause fails
    here and nowhere else — which is why this test exists rather than trusting a
    line of SQL nobody exercises.
    """
    await sign_in(team_world.manager_email)

    rows = (await client.get(DASHBOARD)).json()["gap_analysis"]

    assert rows[-1]["call_type"] == "upsell"
    assert rows[-1]["gap"] is None
    assert rows[-1]["severity"] == "none"


# ── The counted pool reaches the surface intact ───────────────────────────────


@pytest.mark.verifies("AC-TRM-001")
async def test_self_authored_practice_does_not_reach_the_dashboard(
    client, sign_in, team_world
):
    """The top performer's private 2.0 must move nothing here either.

    Asserted at the endpoint and not only at the view, because the exclusion has
    to survive every layer between them. Counted, the team average falls to 6.9
    and the top performer changes.
    """
    await sign_in(team_world.manager_email)

    body = (await client.get(DASHBOARD)).json()

    assert body["team_average"]["score"] == 7.2
    assert body["top_performers"] == 1


# ── An empty month ────────────────────────────────────────────────────────────


@pytest.mark.verifies("FR-TRM-003")
async def test_four_rated_reps_suppress_and_five_do_not(
    client, sign_in, team_world, org_month
):
    """The boundary FR-TRM-003 actually names: fewer than FIVE.

    Both sides, from one team, one month apart. The prior month holds four rated
    reps and the current month five — so an implementation cutting at `< 3` or
    `< 4` passes the suppressed side and fails the tiered one, and `<= 5` fails
    the other way. A pair further apart would leave all of those alive.

    The four prior-month reps all score 6.0, so moving that boundary cost the
    delta assertion nothing: the mean of four 6.0s is the mean of five.
    """
    await sign_in(team_world.manager_email)
    prior = await org_month(months_back=1)

    suppressed = (await client.get(DASHBOARD, params={"month": prior})).json()
    tiered = (await client.get(DASHBOARD)).json()

    assert (suppressed["rated_reps"], suppressed["tiering_suppressed"]) == (4, True)
    assert (suppressed["top_performers"], suppressed["needs_coaching"]) == (0, 0)
    assert (tiered["rated_reps"], tiered["tiering_suppressed"]) == (5, False)
    assert (tiered["top_performers"], tiered["needs_coaching"]) == (1, 1)


async def test_a_team_far_under_the_threshold_also_suppresses(client, sign_in, world):
    """The other branch of `row is None or rated_reps < TIERING_MINIMUM`.

    An empty month short-circuits on the first operand, so before this the second
    was only ever evaluated as False. `world` has two rated reps, so the row
    EXISTS and the comparison actually decides.

    It also checks the one thing the constant's duplication makes possible.
    `TIERING_MINIMUM` lives in Python and V-4 encodes the same 5 in SQL
    (`case when rated_reps < 5 then null`), which a view cannot import. The tier
    counts come from V-4's threshold and the flag from Python's; here they must
    agree that tiering is off.
    """
    """The branch the five-rep world can never reach.

    `tiering_suppressed` is `row is None or rated_reps < TIERING_MINIMUM`. An empty
    month short-circuits on the first operand, and `team_world` always has exactly
    five — so the second operand returning True was never evaluated by any test,
    and `<=`, `>`, or a wrong constant would have gone unnoticed.

    `world` supplies the case for free: two of its reps hold counted attempts, so
    the row EXISTS and carries `rated_reps = 2`.

    It also checks the one thing the constant's duplication makes possible.
    `TIERING_MINIMUM` lives in Python and V-4 encodes the same 5 in SQL
    (`case when rated_reps < 5 then null`), which a view cannot import. If the two
    ever disagreed, this is where it shows: the tier counts come from V-4's
    threshold and the flag comes from Python's, and here they must agree that
    tiering is off.
    """
    await sign_in(world.manager_email)

    body = (await client.get(DASHBOARD)).json()

    assert body["rated_reps"] == 2
    assert body["tiering_suppressed"] is True
    assert (body["top_performers"], body["needs_coaching"]) == (0, 0)


@pytest.mark.verifies("FR-TRM-004")
async def test_a_month_with_no_attempts_renders_empty_not_absent(
    client, sign_in, team_world
):
    """Every required field is present, holding its empty value.

    A team with no counted attempts is ordinary — a new team, or the first day of
    a month. The contract types these nullable rather than optional precisely so
    the client renders an empty state instead of branching on a missing key, and
    `tiering_suppressed` is true because zero rated reps is fewer than five.

    `fn_score_band(null)` returns `'red'`, not null — `null >= 7.5` is null and
    falls through to the else. So an unguarded implementation reports this empty
    month as a red team average, which is worse than wrong: it is alarming.
    """
    await sign_in(team_world.manager_email)

    body = (await client.get(DASHBOARD, params={"month": "2020-01"})).json()

    assert body["month"] == "2020-01"
    assert body["team_average"] is None
    assert body["delta"] is None
    assert body["rated_reps"] == 0
    assert body["tiering_suppressed"] is True
    assert body["top_performers"] == 0
    assert body["needs_coaching"] == 0
    assert body["gap_analysis"] == []


# ── Nothing crosses teams (AC-TRM-006) ────────────────────────────────────────


@pytest.mark.verifies("AC-TRM-006")
async def test_a_second_team_in_the_same_org_reaches_neither_dashboard(
    client, sign_in, team_world
):
    """AC-TRM-006's dashboard third: two managers in ONE org, seeing only their own.

    Tagged `AC-TRM-006` and deliberately NOT `FR-TRM-001`. That requirement reads
    "every surface, list, and computation in this file" — dashboard, roster, deep
    dive, catalog, drill detail and assignment. One of six exists. AC-TRM-006 is
    itself only a third covered here; its catalog and roster clauses arrive with
    those surfaces, and this docstring is the record of which third is done.

    Same org is the case that matters. A second ORG carries a different
    `app.org_id`, so a leak would be caught by the org predicate long before the
    team one — and the team predicate is what this surface rests on. Both
    managers here share `app.org_id` and differ only in `app.team_id`.

    Two things hold it: the RLS policy on `attempt` scopes by
    `current_setting('app.team_id')`, and `_TEAM_MONTH` adds its own
    `cur.team_id = :team`, taken from the session record rather than the request.

    **This test observes the outcome, not which layer produced it.** Removing the
    explicit predicate and re-running leaves every case here green, which was
    checked rather than assumed — so RLS is sufficient on its own, and the
    predicate in the query is the second layer, not the load-bearing one. That is
    the opposite of `/me`, where the explicit filter IS load-bearing because a
    manager legitimately sees their whole team through the same policies.

    Worth stating plainly: if the RLS policy on `attempt` were ever weakened, this
    test would keep passing on the strength of the predicate alone, and vice
    versa. It proves the guarantee, not the mechanism.

    The other team's single attempt scores 1.0, so a dropped predicate is loud:
    the average falls 7.2 -> 6.2 and `rated_reps` goes 5 -> 6.
    """
    await sign_in(team_world.manager_email)
    mine = (await client.get(DASHBOARD)).json()

    assert mine["team_average"] == {"score": 7.2, "band": "amber"}
    assert mine["rated_reps"] == 5

    await sign_in(team_world.other_manager_email)
    theirs = (await client.get(DASHBOARD)).json()

    assert theirs["rated_reps"] == 1, "the other manager saw beyond their own team"
    assert theirs["team_average"] == {"score": 1.0, "band": "red"}
    assert theirs["tiering_suppressed"] is True


# ── The gate and the parameter ────────────────────────────────────────────────


async def test_a_rep_cannot_reach_the_dashboard(client, sign_in, team_world):
    """The role gate from task 0, now on a real route.

    RLS will not do this: a rep reading `v_team_month` is answered, with a team
    average computed from their own attempts alone.

    Deliberately NOT tagged `FR-TRM-001`. That requirement is about operating
    exclusively on the manager's OWN TEAM — cross-team isolation, AC-TRM-006 —
    and proving it needs a second manager's team to fail to see. This proves a
    different thing: that a non-manager is refused at all.
    """
    await sign_in(team_world.rep_emails[0])

    response = await client.get(DASHBOARD)

    assert response.status_code == 404
    assert response.json()["type"] == "/problems/not-found"


async def test_a_malformed_month_is_refused_before_any_query(client, sign_in, team_world):
    """`0000-01` matches the contract's pattern and is still not a month.

    Four digits is four digits, and postgres' minimum year is 1 — so the pattern
    alone cannot make this safe, and `parse_month` answers 422 rather than letting
    a `ValueError` render as a 500.
    """
    await sign_in(team_world.manager_email)

    assert (await client.get(DASHBOARD, params={"month": "sept"})).status_code == 422
    assert (await client.get(DASHBOARD, params={"month": "0000-01"})).status_code == 422
