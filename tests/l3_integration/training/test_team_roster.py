"""`GET /team/roster` — every team rep, sorted by rating (FR-TRM-005).

The arithmetic is already pinned in `test_trm_formulas.py`; the dashboard already
proves the month anchor and the gate work. What is new here is the word **every**.

FR-TRM-005 says the roster lists every team rep. A rep with no counted attempts
this month has no row in V-2 and therefore none in V-4 — so a roster built by
reading the tier view would silently omit exactly the person a manager most needs
to notice, and every assertion about the five who DO have attempts would still
pass. The roster is therefore a left join FROM `account`, and
`test_a_rep_with_no_counted_attempts_still_appears` is the test that forces it.

## One clause of FR-TRM-005 is claimed on an affordance, not an end-to-end path

The requirement ends "— each row opening that rep's deep dive", and the deep dive
(FR-TRM-006, `GET /team/reps/{account_id}`) does not exist yet. What the roster
owes that clause is the rep's identity, and `account_id` is asserted on every row
against the seeded reps. What cannot be shown here is that the identifier
actually resolves at the other end.

The tag is claimed anyway, because the roster delivers everything the roster owns
and the target is a separate requirement. The debt is specific and small: when the
deep dive lands, a test should follow a roster row's `account_id` to it. If that
turns out not to work, this claim was wrong and should be reopened.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.l3_integration]

ROSTER = "/api/v1/team/roster"




# ── The rows, and their order ─────────────────────────────────────────────────


@pytest.mark.verifies("FR-TRM-005")
async def test_the_roster_lists_every_rep_in_rating_order(
    client, sign_in, team_world, org_month
):
    """Rating descending, and the tier pill beside it.

    The same five ratings the formula tests pin, now carrying the tier that
    V-4 assigned — so a roster that computed its own ranking rather than reading
    the view would disagree here rather than quietly showing a different order
    from the dashboard's counts.
    """
    await sign_in(team_world.manager_email)

    body = (await client.get(ROSTER)).json()

    assert body["month"] == await org_month()
    rated = [row for row in body["data"] if row["rating"] is not None]
    assert [(r["rating"]["score"], r["tier"]) for r in rated] == [
        (8.1, "top"),
        (7.6, "mid"),
        (7.0, "mid"),
        (6.7, "mid"),
        (6.5, "needs_coaching"),
    ]
    assert [r["account_id"] for r in rated] == [str(rep) for rep in team_world.reps]


@pytest.mark.verifies("FR-TRM-005")
async def test_a_rep_with_no_counted_attempts_still_appears(client, sign_in, team_world):
    """The word the whole endpoint turns on: EVERY team rep.

    This rep has no row in V-2 and none in V-4. A roster read off the tier view
    omits them entirely — and nothing else in this file would notice, because the
    five rated reps are all still correct.

    They carry nulls rather than zeros for rating and tier, because "not measured"
    is not "measured at zero". `counted_attempts` IS zero, since that is a count
    and zero is the true one.
    """
    await sign_in(team_world.manager_email)

    body = (await client.get(ROSTER)).json()
    unrated = [row for row in body["data"] if row["account_id"] == str(team_world.unrated_rep)]

    assert len(unrated) == 1, "a rep with no attempts was dropped from the roster"
    assert unrated[0]["rating"] is None
    assert unrated[0]["tier"] is None
    assert unrated[0]["counted_attempts"] == 0
    assert unrated[0]["strongest_call_type"] is None
    assert unrated[0]["weakest_call_type"] is None


@pytest.mark.verifies("FR-TRM-005")
async def test_unrated_reps_sort_after_every_rated_one(client, sign_in, team_world):
    """`nulls last` again, for the same reason it mattered on the gap rows.

    PostgreSQL sorts NULLs FIRST under `desc`, so without the override the reps
    with no measurement head a list ordered best-first — reading as the team's
    strongest performers.

    Asserted as a partition rather than by naming the last row. There are two
    unrated reps now (one never measured, one deactivated) and `a.id` decides
    their order between themselves — which is deterministic per run but depends on
    minted UUIDs, so pinning a specific tail row would be a coin flip.
    """
    await sign_in(team_world.manager_email)

    rows = (await client.get(ROSTER)).json()["data"]
    rated = [i for i, row in enumerate(rows) if row["rating"] is not None]
    unrated = [i for i, row in enumerate(rows) if row["rating"] is None]

    assert rated and unrated, "the fixture must hold both kinds for this to mean anything"
    assert max(rated) < min(unrated)
    assert rows[0]["rating"]["score"] == 8.1


@pytest.mark.verifies("FR-TRM-005")
async def test_a_deactivated_rep_is_still_on_the_roster(client, sign_in, team_world):
    """No status filter, and this is what holds that line.

    FR-IDA-010 refuses a deactivated account's authentication and preserves its
    records; it does not take the person off the team. V-1, V-2 and V-4 apply no
    status filter either, so a rep deactivated mid-month still counts toward
    `rated_reps` and the team average on the dashboard.

    A roster that filtered `status = 'active'` would therefore report a different
    population from the surface FR-TRM-005 places it directly below — "5 rated
    reps" above a shorter table, with nothing on either explaining the difference.

    **Bound of this test.** The deactivated rep here holds no attempts, so it
    proves the predicate is absent but not the harder consistency claim: that a
    deactivated rep WITH attempts appears on both surfaces. Seeding that would
    make `rated_reps` six, moving the tier split, the bottom-half cohort and every
    gap — a large recomputation for a predicate that is simply not there.
    """
    await sign_in(team_world.manager_email)

    rows = (await client.get(ROSTER)).json()["data"]
    deactivated = [
        row for row in rows if row["account_id"] == str(team_world.deactivated_rep)
    ]

    assert len(deactivated) == 1, "a deactivated rep was filtered off the roster"
    assert deactivated[0]["display_name"] == "Deactivated Rep"
    assert deactivated[0]["rating"] is None


@pytest.mark.verifies("FR-TRM-005")
async def test_each_row_carries_the_reps_display_name(client, sign_in, team_world):
    """The roster is read by a human choosing who to coach.

    Names come from `account`, which the manager reads through
    `account_manager_team_read` — the same policy that scopes the join to their
    own team.
    """
    await sign_in(team_world.manager_email)

    rows = (await client.get(ROSTER)).json()["data"]

    assert [r["display_name"] for r in rows[:5]] == [
        "Rep 1",
        "Rep 2",
        "Rep 3",
        "Rep 4",
        "Rep 5",
    ]


# ── Strongest and weakest ─────────────────────────────────────────────────────


@pytest.mark.verifies("FR-TRM-005")
async def test_strongest_and_weakest_call_types_are_per_rep(client, sign_in, team_world):
    """Not the same answer for everybody, which is the point of asserting all five.

    Reps 1-3 are strongest at discovery; reps 4 and 5 at post-proposal. An
    implementation that returned the first non-null column, or the team's
    strongest type, agrees with the first three rows and fails on the last two.

    Renewal is weakest for all five — that is the fixture's shape, not a
    coincidence worth hiding, and it is what makes the dashboard's renewal gap the
    narrowest while still being everyone's worst.
    """
    await sign_in(team_world.manager_email)

    rows = (await client.get(ROSTER)).json()["data"]

    assert [(r["strongest_call_type"], r["weakest_call_type"]) for r in rows[:5]] == [
        ("discovery", "renewal"),
        ("discovery", "renewal"),
        ("discovery", "renewal"),
        ("post_proposal", "renewal"),
        ("post_proposal", "renewal"),
    ]


@pytest.mark.verifies("AC-TRP-004")
async def test_self_authored_practice_moves_no_row(client, sign_in, team_world):
    """The top performer's private 2.0 is on a DISCOVERY drill.

    Counted, it would drag their discovery rating from 9.0 to 5.5 and make
    discovery their WEAKEST type instead of their strongest — so the exclusion is
    observable here in a way the overall rating alone would not show.
    """
    await sign_in(team_world.manager_email)

    rows = (await client.get(ROSTER)).json()["data"]

    assert rows[0]["strongest_call_type"] == "discovery"
    assert rows[0]["counted_attempts"] == 3


# ── Suppression, scope, and the gate ──────────────────────────────────────────


@pytest.mark.verifies("FR-TRM-003")
async def test_tier_pills_are_null_while_tiering_is_suppressed(client, sign_in, world):
    """Below five rated reps every pill is blank, and the ratings still show.

    FR-TRM-003 leaves "ratings shown untiered" rather than hiding the roster —
    the split stops carrying meaning, the measurements do not.
    """
    await sign_in(world.manager_email)

    rows = (await client.get(ROSTER)).json()["data"]

    assert [r["tier"] for r in rows] == [None, None]
    assert all(r["rating"] is not None for r in rows)


@pytest.mark.verifies("AC-TRM-006")
async def test_the_other_teams_reps_are_absent(client, sign_in, team_world):
    """AC-TRM-006's roster clause — the one the dashboard could not cover.

    A dashboard leak shows up as a moved average; a roster leak shows up as
    another manager's rep NAMED on the page. This is the surface where the
    concealment rule is legible.
    """
    await sign_in(team_world.manager_email)

    ids = [r["account_id"] for r in (await client.get(ROSTER)).json()["data"]]

    assert str(team_world.other_rep) not in ids
    assert str(team_world.other_manager) not in ids


async def test_the_manager_is_not_on_their_own_roster(client, sign_in, team_world):
    """A manager shares a `team_id` with their reps, so the join reaches them too.

    They are not a rep and hold no counted attempts, so an unfiltered roster would
    show the manager to themselves as an unrated row — harmless-looking, and
    wrong: the roster is who they coach.
    """
    await sign_in(team_world.manager_email)

    ids = [r["account_id"] for r in (await client.get(ROSTER)).json()["data"]]

    assert str(team_world.manager) not in ids


async def test_a_rep_cannot_reach_the_roster(client, sign_in, team_world):
    """The role gate, on the surface that names individuals."""
    await sign_in(team_world.rep_emails[0])

    response = await client.get(ROSTER)

    assert response.status_code == 404
    assert response.json()["type"] == "/problems/not-found"
