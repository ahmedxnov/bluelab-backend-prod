"""Per-drill history — `GET /drills/{id}/my-history` (FR-TRP-011/012, FR-SCR-015).

Written before the endpoint, from the acceptance criteria rather than from the
query. AC-TRP-006 states the shape of the proof:

    Given four graded attempts on one drill
    When the rep opens its history
    Then best, latest, average, and trend match the primitives' math
    And each listed attempt opens its own review.

So the world seeds exactly four, with **four different answers** — best 9.0,
latest 2.0, average 5.0, first 4.0 — because a fixture where two of those
coincide cannot tell a correct query from one that swapped them. A fifth attempt
is ungraded, and it is the one that says statistics and the attempt list are
different sets: it belongs in the list and nowhere near the arithmetic.

The drill is self-authored on purpose. FR-TRP-010 excludes private practice from
every statistic *beyond the drill's own history*, and this endpoint is that
exception — so the same rows that must never move a rating must appear here in
full.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

pytestmark = [
    pytest.mark.l3_integration,
    pytest.mark.l7_security,
    pytest.mark.invariant_path,
]


def _history(drill_id) -> str:
    return f"/api/v1/drills/{drill_id}/my-history"


# ── the statistics primitives (AC-TRP-006, FR-SCR-015) ───────────────────────


@pytest.mark.verifies("FR-TRP-011", "FR-SCR-015", "AC-TRP-006")
async def test_the_four_primitives_match_the_seeded_arithmetic(client, sign_in, world):
    """Four graded attempts: 4.0, 9.0, 5.0, 2.0 oldest to newest.

    best 9.0 — the maximum, not the most recent.
    latest 2.0 — the most recent, not the maximum.
    average 5.0 — the mean of all four.
    trend -2.0 — latest minus FIRST (2.0 - 4.0), and negative, so a query that
    subtracted the other way round reports +2.0 and reads as improvement.
    """
    await sign_in()

    response = await client.get(_history(world.self_drill))

    assert response.status_code == 200, response.text
    stats = response.json()["stats"]
    assert stats["best"] == 9.0
    assert stats["latest"] == 2.0
    assert stats["average"] == 5.0
    assert stats["trend"] == -2.0
    assert stats["graded_attempts"] == 4


@pytest.mark.verifies("FR-SCR-015")
async def test_an_ungraded_attempt_is_listed_but_not_counted(client, sign_in, world):
    """The fifth attempt is `grading_pending` and the newest.

    It heads the list with a null score, and `graded_attempts` stays 4. The two
    are different sets — the list is every attempt, the statistics are only the
    graded ones — and conflating them is the easiest mistake here.
    """
    await sign_in()

    body = (await client.get(_history(world.self_drill))).json()

    assert body["stats"]["graded_attempts"] == 4
    assert len(body["attempts"]) == 5
    assert body["attempts"][0]["status"] == "grading_pending"
    assert body["attempts"][0]["score"] is None


# NOT tagged FR-SCR-005. That requirement is the banding RULE — green ≥ 7.5,
# amber ≥ 5.5, red below — and two sample points do not verify a rule with two
# thresholds. What this checks is the weaker, still worth-checking property that a
# score never travels without its band. The rule itself wants boundary cases at
# 7.49/7.5 and 5.49/5.5 against `fn_score_band`, which belongs with the V-1…V-11
# formula tests that do not exist yet.
async def test_every_score_travels_with_its_band(client, sign_in, world):
    """9.0 bands green, 2.0 bands red — clients render, never re-derive."""
    await sign_in()

    body = (await client.get(_history(world.self_drill))).json()
    scored = [a["score"] for a in body["attempts"] if a["score"] is not None]

    assert {"score": 9.0, "band": "green"} in scored
    assert {"score": 2.0, "band": "red"} in scored


# ── the attempt list (FR-TRP-011/012) ────────────────────────────────────────


@pytest.mark.verifies("FR-TRP-011")
async def test_attempts_are_listed_newest_first(client, sign_in, world):
    await sign_in()

    body = (await client.get(_history(world.self_drill))).json()
    started = [a["started_at"] for a in body["attempts"]]

    assert started == sorted(started, reverse=True)


@pytest.mark.verifies("FR-TRP-011")
async def test_attempt_number_is_chronological_regardless_of_list_order(
    client, sign_in, world
):
    """The list runs newest first; the numbering runs oldest first.

    `attempt_number` is not stored — `attempt` has no such column — so it is
    derived, and the two orderings pull in opposite directions. Numbering by
    position in the returned list would produce 1 for the newest attempt, which
    is the reverse of what a chart reads when it reverses the list.

    Tagged FR-TRP-011 (the attempt list), NOT FR-TRP-012. That one requires the
    history to *render* a tier-coloured progression chart, and a chart is not
    something a backend test can witness — the contract even says the points
    "derive client-side by reversing graded attempts". What is verified here is
    the ordering contract the chart depends on, which is a precondition for
    FR-TRP-012 rather than the requirement itself.
    """
    await sign_in()

    attempts = (await client.get(_history(world.self_drill))).json()["attempts"]

    assert [a["attempt_number"] for a in attempts] == [5, 4, 3, 2, 1]
    oldest = attempts[-1]
    assert oldest["attempt_number"] == 1
    assert oldest["score"] == {"score": 4.0, "band": "red"}


@pytest.mark.verifies("FR-TRP-011")
async def test_each_attempt_carries_the_id_its_review_opens(client, sign_in, world):
    """AC-TRP-006's second clause — "each listed attempt opens its own review".

    The review is a separate operation; what this endpoint owes it is a distinct,
    non-null `attempt_id` per row.
    """
    await sign_in()

    attempts = (await client.get(_history(world.self_drill))).json()["attempts"]
    ids = [a["attempt_id"] for a in attempts]

    assert all(ids)
    assert len(set(ids)) == len(ids)


# ── empty and boundary states ────────────────────────────────────────────────


@pytest.mark.verifies("FR-TRP-011")
async def test_a_visible_drill_never_attempted_is_an_empty_history_not_a_404(
    client, sign_in, world
):
    """`other_rep` can see the team drill and has never touched it.

    Null statistics with an empty list, not an error: nothing is missing, the
    answer is that there is nothing yet. A 404 here would tell a rep their own
    library card leads nowhere.
    """
    await sign_in(world.other_rep_email)

    response = await client.get(_history(world.drill_renewal))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["attempts"] == []
    assert body["stats"] == {
        "best": None,
        "latest": None,
        "average": None,
        "trend": None,
        "graded_attempts": 0,
    }


@pytest.mark.verifies("FR-SCR-015")
async def test_a_single_graded_attempt_has_a_trend_of_zero_not_null(
    client, sign_in, world
):
    """Renewal has exactly one graded attempt, 6.0.

    Latest and first are the same row, so the trend is 0.0 — measured and flat.
    Null would mean "not measured", which is what a rep with no attempts gets,
    and the two states must not render the same.
    """
    await sign_in()

    stats = (await client.get(_history(world.drill_renewal))).json()["stats"]

    assert stats["graded_attempts"] == 1
    assert stats["best"] == 6.0
    assert stats["latest"] == 6.0
    assert stats["trend"] == 0.0


# ── access (ADR-0036 denial as absence, FR-SCR-018) ──────────────────────────


async def test_an_unknown_drill_is_not_found(client, sign_in):
    await sign_in()

    assert (await client.get(_history(uuid4()))).status_code == 404


@pytest.mark.verifies("FR-SCR-018", "AC-TRP-004")
async def test_a_teammates_self_authored_drill_is_not_found(client, sign_in, world):
    """Absence and "not yours" answer identically (ADR-0036).

    `other_rep` cannot see this drill at all, so distinguishing the two would turn
    the endpoint into a probe for which drill ids exist — and worse, for which of
    them are somebody's private practice.
    """
    await sign_in(world.other_rep_email)

    assert (await client.get(_history(world.self_drill))).status_code == 404


@pytest.mark.verifies("AC-TRP-004")
async def test_a_manager_cannot_reach_a_reps_private_practice(client, sign_in, world):
    await sign_in(world.manager_email)

    assert (await client.get(_history(world.self_drill))).status_code == 404


@pytest.mark.verifies("FR-DRL-016", "AC-DRL-007")
async def test_archival_withdraws_the_drill_from_the_rep_but_not_from_the_manager(
    client, sign_in, world
):
    """Both halves of FR-DRL-016, which pull in opposite directions.

    "Withdraw it from all availability — libraries…" is the rep's half: an
    archived drill leaves their world entirely, so its history is a 404 for them,
    exactly as its card is gone from the grid. Their individual reviews survive —
    those key on `attempt_id`, not on the drill.

    "…while preserving every existing attempt, review, and statistic" is the
    manager's half, and AC-DRL-007 is written from their side: a manager archives
    it, and the statistics "remain intact and viewable". So the record is reachable
    for them.

    This test was written asserting the rep could still read it, and the endpoint
    disagreed. The endpoint was right — `app_drill_readable_published` requires
    `status = 'published'`, and "preserved" in FR-DRL-016 means the rows survive,
    not that a withdrawn drill stays on the surface it was withdrawn from.
    """
    await sign_in()
    assert (await client.get(_history(world.archived_drill))).status_code == 404

    await sign_in(world.manager_email)
    response = await client.get(_history(world.archived_drill))
    assert response.status_code == 200, response.text
    assert response.json()["stats"]["graded_attempts"] == 0


@pytest.mark.verifies("FR-TRP-011")
async def test_the_history_is_the_callers_own_and_not_the_teams(client, sign_in, world):
    """A manager reading V-8 sees their whole team through it.

    So the manager's history on a drill their reps have attempted must be empty —
    they have not attempted it. Without an explicit account predicate the view
    reports the team's numbers as the caller's own, which is the `/me` trap in
    its purest form: a plausible answer that belongs to somebody else.
    """
    await sign_in(world.manager_email)

    body = (await client.get(_history(world.drill_discovery))).json()

    assert body["stats"]["graded_attempts"] == 0
    assert body["attempts"] == []


async def test_a_malformed_drill_id_is_refused(client, sign_in):
    await sign_in()

    assert (await client.get("/api/v1/drills/not-a-uuid/my-history")).status_code == 422


async def test_the_history_requires_a_session(client, world):
    assert (await client.get(_history(world.drill_renewal))).status_code == 401
