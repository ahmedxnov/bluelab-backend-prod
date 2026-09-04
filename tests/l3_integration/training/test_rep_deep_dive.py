"""`GET /team/reps/{account_id}` — one rep's month (FR-TRM-006).

The first route on this plane to take a caller-supplied id, which makes it the
first that can be asked about somebody it should refuse. Everything before it was
scoped entirely by the session: there was no parameter to point somewhere else.

Two properties carry most of the weight here.

**The refusal is the generic 404.** A manager naming a rep on another team must
not learn whether that rep exists (AC-IDA-006, ADR-0036 §2). `team_world` seeds a
second manager and rep in the SAME org for exactly this.

**`attempt_number` spans the drill's history, not the month.** FR-TRP-002 requires
the manager and the rep to read the same numbers, and the rep's own history
numbers every attempt on a drill from the oldest. A window restricted to the month
would restart at 1 and quietly disagree. Rep 1 holds a prior-month discovery
attempt, so this month's discovery attempt is number 2 — and a month-scoped window
reports it as 1. Verified by making the window month-scoped and watching exactly
this file's numbering test fail.

## What is tagged here, and what is deliberately not

`FR-TRM-007` is NOT claimed. It reads "the manager shall open any counted team
attempt's review read-only in replay chrome... with rubric weights visible", and
`GET /attempts/{id}/review` does not exist. What this endpoint supplies is the
`attempt_id` that opens it and the number its chrome will display — the ingredients,
not the surface. The requirement becomes claimable when the review does.

`FR-TRM-006` IS claimed, and its closing clause — "each opening its replay" — rests
on the same affordance argument FR-TRM-005 was claimed on last task: the row
carries the identifier, and whether it resolves cannot be shown until the target
exists. That debt was recorded rather than buried, and
`test_a_roster_rows_account_id_opens_that_reps_deep_dive` below pays it off for the
roster. The equivalent test for the replay belongs with the review endpoint, and
if the `attempt_id` turns out not to resolve there, this claim was wrong and should
be reopened.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.l3_integration, pytest.mark.l7_security]

DEEP_DIVE = "/api/v1/team/reps"
ROSTER = "/api/v1/team/roster"




# ── The rep's month ───────────────────────────────────────────────────────────


@pytest.mark.verifies("FR-TRM-006")
async def test_the_deep_dive_reports_the_reps_rating_and_trend(
    client, sign_in, team_world, org_month
):
    """8.1 this month against 6.0 last, so the trend is +2.1.

    The same 8.1 the roster shows and the formula tests pin — FR-TRP-002's "one
    truth with the manager" is a claim about the pool and the formula, so a deep
    dive computing its own average would disagree here rather than somewhere a
    rep would have to notice first.
    """
    await sign_in(team_world.manager_email)

    response = await client.get(f"{DEEP_DIVE}/{team_world.reps[0]}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["account_id"] == str(team_world.reps[0])
    assert body["display_name"] == "Rep 1"
    assert body["month"] == await org_month()
    assert body["rating"] == {"score": 8.1, "band": "green"}
    assert body["trend"] == 2.1


@pytest.mark.verifies("FR-TRM-006")
async def test_a_rep_without_a_prior_month_has_no_trend(client, sign_in, team_world):
    """Null, not zero — they have not been measured twice.

    The prior month was seeded for four of the five reps precisely so this case
    exists alongside the other. Rep 5 is the one left out.
    """
    await sign_in(team_world.manager_email)

    body = (await client.get(f"{DEEP_DIVE}/{team_world.reps[4]}")).json()

    assert body["rating"] == {"score": 6.5, "band": "amber"}
    assert body["trend"] is None


# ── The per-call-type profile ─────────────────────────────────────────────────


@pytest.mark.verifies("FR-TRM-006")
async def test_the_profile_covers_all_four_call_types(client, sign_in, team_world):
    """Including the one they have never attempted.

    Deliberately unlike the dashboard's gap rows, which omit a call type that
    cannot be compared. A profile is a picture of the whole rep, and an omitted
    upsell row would hide exactly the call type this rep has been avoiding — the
    same reasoning the rep's own call-type cards are built on.

    Untried reads as `rating: null, counted_attempts: 0`. Zero attempts is a true
    count; a null rating is the absence of a measurement, and the two say
    different things.
    """
    await sign_in(team_world.manager_email)

    profile = (await client.get(f"{DEEP_DIVE}/{team_world.reps[0]}")).json()["per_call_type"]

    assert [(p["call_type"], p["counted_attempts"]) for p in profile] == [
        ("discovery", 1),
        ("post_proposal", 1),
        ("renewal", 1),
        ("upsell", 0),
    ]
    assert [p["rating"] for p in profile] == [
        {"score": 9.0, "band": "green"},
        {"score": 8.2, "band": "green"},
        {"score": 7.0, "band": "amber"},
        None,
    ]


@pytest.mark.verifies("AC-TRP-004")
async def test_self_authored_practice_reaches_no_part_of_the_deep_dive(
    client, sign_in, team_world
):
    """The private 2.0 is on a discovery drill, and this is a manager's surface.

    Counted, it would drag the discovery rating from 9.0 to 5.5, add a fourth
    counted attempt, and put a drill the manager must never see into
    `recent_attempts`. All three are asserted, because the concealment has to hold
    in every part of the response and not merely in the headline rating.
    """
    await sign_in(team_world.manager_email)

    body = (await client.get(f"{DEEP_DIVE}/{team_world.reps[0]}")).json()
    discovery = next(p for p in body["per_call_type"] if p["call_type"] == "discovery")

    assert discovery["rating"]["score"] == 9.0
    assert discovery["counted_attempts"] == 1
    assert len(body["recent_attempts"]) == 3
    assert all("self" not in a["drill_label"] for a in body["recent_attempts"])


# ── Recent attempts, and their numbers ────────────────────────────────────────


@pytest.mark.verifies("FR-TRM-006")
async def test_recent_attempts_are_newest_first_and_carry_their_replay_id(
    client, sign_in, team_world
):
    """Newest first, each naming the drill and carrying the id its replay opens.

    Rep 1's three counted attempts this month run discovery 01:00, post-proposal
    02:00, renewal 03:00 — so newest-first is the reverse of the seeding order,
    which a query that forgot `desc` would get exactly backwards.
    """
    await sign_in(team_world.manager_email)

    attempts = (await client.get(f"{DEEP_DIVE}/{team_world.reps[0]}")).json()["recent_attempts"]

    assert [a["call_type"] for a in attempts] == ["renewal", "post_proposal", "discovery"]
    assert [a["drill_label"] for a in attempts] == [
        "team renewal",
        "team post_proposal",
        "team discovery",
    ]
    assert [a["score"] for a in attempts] == [
        {"score": 7.0, "band": "amber"},
        {"score": 8.2, "band": "green"},
        {"score": 9.0, "band": "green"},
    ]
    assert all(a["attempt_id"] for a in attempts)


@pytest.mark.verifies("FR-TRM-006")
async def test_attempt_numbers_span_the_drills_history_not_the_month(
    client, sign_in, team_world
):
    """The number is the attempt's place in that DRILL, counted from the oldest.

    Rep 1 attempted the discovery drill last month as well, so this month's
    discovery attempt is their SECOND on it. The renewal and post-proposal drills
    they have attempted once, so those are number 1.

    A window partitioned over the month alone renumbers every drill from 1 and
    returns `[1, 1, 1]` here — self-consistent, and disagreeing with the number
    the rep reads in their own drill history for the very same attempt
    (FR-TRP-002).
    """
    await sign_in(team_world.manager_email)

    attempts = (await client.get(f"{DEEP_DIVE}/{team_world.reps[0]}")).json()["recent_attempts"]

    assert [(a["call_type"], a["attempt_number"]) for a in attempts] == [
        ("renewal", 1),
        ("post_proposal", 1),
        ("discovery", 2),
    ]


# ── Who may be asked about ────────────────────────────────────────────────────


@pytest.mark.verifies("AC-TRM-006", "AC-IDA-006")
async def test_another_teams_rep_is_not_found(client, sign_in, team_world):
    """The first id a caller can point somewhere it should not go.

    Same org, different team. The answer must be the generic `not-found`, not a
    403 — a 403 would confirm the rep exists and that this manager merely may not
    see them, which is the disclosure ADR-0036 §2 closed everywhere else.
    """
    await sign_in(team_world.manager_email)

    response = await client.get(f"{DEEP_DIVE}/{team_world.other_rep}")

    assert response.status_code == 404
    assert response.json()["type"] == "/problems/not-found"


@pytest.mark.verifies("AC-IDA-006")
async def test_an_unknown_id_is_indistinguishable_from_another_teams_rep(
    client, sign_in, team_world
):
    """Byte-equality apart from `request_id`, which is per-request by design.

    This is the assertion that makes the previous one mean something: if absence
    and denial rendered differently, a manager could tell a rep who does not exist
    from one they are not allowed to see, and enumerate the org by the difference.
    """
    await sign_in(team_world.manager_email)

    denied = (await client.get(f"{DEEP_DIVE}/{team_world.other_rep}")).json()
    absent = (
        await client.get(f"{DEEP_DIVE}/00000000-0000-0000-0000-000000000000")
    ).json()

    assert denied.pop("request_id", None) is not None
    absent.pop("request_id", None)
    assert denied == absent


async def test_a_manager_cannot_deep_dive_themselves(client, sign_in, team_world):
    """A manager shares their team's `team_id`, so the row is reachable by id.

    They are not a rep, and a deep dive of a manager is not a thing this surface
    describes — so it answers not-found rather than an empty profile that looks
    like a rep who never practised.
    """
    await sign_in(team_world.manager_email)

    response = await client.get(f"{DEEP_DIVE}/{team_world.manager}")

    assert response.status_code == 404


async def test_a_rep_cannot_deep_dive_anyone(client, sign_in, team_world):
    """The role gate, including on themselves.

    A rep reads their own numbers through `/me`, which is the same pool and the
    same formulas. This surface is the manager's framing of somebody else.
    """
    await sign_in(team_world.rep_emails[0])

    response = await client.get(f"{DEEP_DIVE}/{team_world.reps[0]}")

    assert response.status_code == 404


# ── The link the roster promised ──────────────────────────────────────────────


@pytest.mark.verifies("FR-TRM-005")
async def test_a_roster_rows_account_id_opens_that_reps_deep_dive(
    client, sign_in, team_world
):
    """Closes the debt FR-TRM-005 was claimed on.

    That requirement ends "each row opening that rep's deep dive", and when the
    roster shipped there was nothing to open — the claim rested on the row
    carrying `account_id`, with a note that this test should follow it once the
    deep dive existed. It does now.
    """
    await sign_in(team_world.manager_email)

    rows = (await client.get(ROSTER)).json()["data"]
    top = rows[0]

    body = (await client.get(f"{DEEP_DIVE}/{top['account_id']}")).json()

    assert body["account_id"] == top["account_id"]
    assert body["display_name"] == top["display_name"]
    assert body["rating"] == top["rating"]
