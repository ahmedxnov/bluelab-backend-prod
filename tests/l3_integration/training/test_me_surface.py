"""The rep surface — progress, profile, and the coaching feed.

Assertions here are derived from the contract and the FRs, not from reading the
implementation. That distinction matters more than usual: this code was written
before its tests, and a retrofitted suite that mirrors the code it was written
against proves only that the code does what it does.

The property this suite exists for is **the counted pool**. Almost every number
on this surface comes from `v_counted_attempt`, which excludes self-authored
practice — AC-TRP-004, a rep's private rehearsal staying private. The world seeds
a graded self-authored attempt scoring 2.0 specifically so that a regression
dropping that predicate moves every rating and fails loudly rather than quietly
lowering someone's score.
"""

from __future__ import annotations

from datetime import datetime

import pytest

pytestmark = [
    pytest.mark.l3_integration,
    pytest.mark.l7_security,
    pytest.mark.invariant_path,
]

PROGRESS = "/api/v1/me/progress"
PROFILE = "/api/v1/me/profile"
FEEDBACK = "/api/v1/me/coach-feedback"
MARK_READ = "/api/v1/me/coach-feedback/mark-read"


# ── GET /me/progress ──────────────────────────────────────────────────────────


@pytest.mark.verifies("FR-TRP-001")
async def test_progress_reports_this_months_rating_with_its_band(client, sign_in):
    """A score never travels without its band (FR-SCR-005).

    Seeded: discovery 7.6 and 8.4, renewal 6.0 — mean 7.333, which rounds to 7.3
    and bands amber. The self-authored 2.0 is excluded; had it counted the mean
    would be 6.0.
    """
    await sign_in()

    response = await client.get(PROGRESS)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rating"] == {"score": 7.3, "band": "amber"}


@pytest.mark.verifies("AC-TRP-004")
async def test_self_authored_practice_never_reaches_the_rating(client, sign_in):
    """The predicate this whole surface leans on.

    The seeded self-authored attempt scores 2.0 — far below the others — so if it
    ever entered the counted pool the monthly rating drops from 7.3 to 6.0 and
    this fails. That is the point: a rep's private rehearsal must not move the
    number their manager sees.
    """
    await sign_in()

    body = (await client.get(PROGRESS)).json()

    assert body["rating"]["score"] == 7.3, "self-authored practice entered the rating"
    discovery = next(c for c in body["call_type_cards"] if c["call_type"] == "discovery")
    assert discovery["attempts"] == 2, "the self-authored discovery attempt was counted"


@pytest.mark.verifies("FR-TRP-001")
async def test_all_four_call_types_are_returned_weakest_first(client, sign_in):
    """A card set that omitted untried types would hide exactly the call type a
    rep has been avoiding."""
    await sign_in()

    cards = (await client.get(PROGRESS)).json()["call_type_cards"]

    assert {c["call_type"] for c in cards} == {
        "discovery", "post_proposal", "renewal", "upsell",
    }
    assert cards[0]["call_type"] == "renewal", "6.0 is weaker than discovery's 8.0"
    assert cards[0]["weakest"] is True
    assert sum(c["weakest"] for c in cards) == 1, "exactly one card is tagged"


@pytest.mark.verifies("FR-TRP-001")
async def test_an_untried_call_type_is_unmeasured_not_weakest(client, sign_in):
    """Untried sorts LAST despite having no score.

    Sorting a null rating as the lowest value would tag the call type the product
    knows least about and send the rep to work on it. Unmeasured is not bad.
    """
    await sign_in()

    cards = (await client.get(PROGRESS)).json()["call_type_cards"]
    untried = [c for c in cards if c["rating"] is None]

    assert {c["call_type"] for c in untried} == {"post_proposal", "upsell"}
    assert all(c["attempts"] == 0 for c in untried)
    assert all(c["weakest"] is False for c in untried)
    assert [c["call_type"] for c in cards[-2:]] == [
        c["call_type"] for c in untried
    ], "an untried type outranked a measured one"


@pytest.mark.verifies("FR-TRP-001")
async def test_a_first_month_has_no_direction(client, sign_in):
    """Null, not `flat`.

    `flat` is a finding — the rep held steady. A rep with no prior month has not
    been flat; they have not been measured twice, and rendering an arrow there
    would report a trend that does not exist.
    """
    await sign_in()

    body = (await client.get(PROGRESS)).json()

    assert body["delta"] is None
    assert body["direction"] is None


@pytest.mark.verifies("FR-TRP-001")
async def test_the_call_type_filter_narrows_the_trend_but_not_the_rating(client, sign_in):
    """Switching the filter must not appear to change where the rep stands.

    The headline rating is all-types by contract; only the weekly trend is scoped.
    If the filter reached the rating, a rep could 'improve' by selecting their
    best call type.
    """
    await sign_in()

    unfiltered = (await client.get(PROGRESS)).json()
    renewal = (await client.get(PROGRESS, params={"call_type": "renewal"})).json()

    assert renewal["rating"] == unfiltered["rating"], "the filter moved the headline rating"

    # Asserted on properties that hold whatever day the suite runs.
    #
    # An earlier version pinned the exact buckets — `unfiltered == [7.3]` — which
    # assumed the three ordered attempts always fall in one ISO week. They need
    # not: the current-month fixture can straddle a week boundary. An exact bucket
    # assertion would therefore be date-dependent.
    #
    # Renewal has exactly ONE attempt, so its series is a single 6.0 point no
    # matter how the weeks fall. The unfiltered series covers three attempts
    # across two call types, so it cannot be identical.
    assert [p["score"] for p in renewal["weekly_trend"]] == [6.0], (
        "non-renewal attempts reached the filtered trend"
    )
    assert unfiltered["weekly_trend"] != renewal["weekly_trend"], (
        "the filter narrowed nothing"
    )


@pytest.mark.verifies("FR-TRP-001")
async def test_filtering_to_an_untried_call_type_empties_the_trend_not_the_rating(
    client, sign_in, world
):
    """An empty weekly trend is a normal answer, not an error.

    `other_rep` has one discovery attempt and no upsell ones, so filtering to
    upsell must produce an empty series while the headline rating — which is
    all-types by contract — stays exactly where it was.
    """
    await sign_in(world.other_rep_email)

    unfiltered = (await client.get(PROGRESS)).json()
    upsell = (await client.get(PROGRESS, params={"call_type": "upsell"})).json()

    assert upsell["weekly_trend"] == []
    assert upsell["rating"] == unfiltered["rating"] is not None, "the filter emptied the rating"


@pytest.mark.verifies("CMP-004", "AC-TRP-004")
async def test_a_managers_own_progress_excludes_their_teams_attempts(client, sign_in, world):
    """The one place RLS is NOT sufficient on its own, pinned.

    Through `v_counted_attempt` a manager legitimately sees their whole team —
    that is what the team surface is built on. So on `/me`, where the answer must
    be about the caller alone, the explicit `rep_account_id` filter is
    load-bearing: without it a manager's personal rating becomes their team's
    average, reported as their own.

    The manager here has run no drills, so anything other than an empty rating
    means somebody else's attempts were counted as theirs.
    """
    await sign_in(world.manager_email)

    body = (await client.get(PROGRESS)).json()

    assert body["rating"] is None, "the manager's team was aggregated into their own rating"
    assert all(c["attempts"] == 0 for c in body["call_type_cards"])


@pytest.mark.verifies("CMP-004")
async def test_progress_shows_only_the_callers_own_attempts(client, sign_in, world):
    """`other_rep` scored 9.9; the caller's rating is 7.3.

    The views are `security_invoker`, so this is RLS doing the work rather than a
    `where` clause somebody remembered to write.
    """
    await sign_in()
    mine = (await client.get(PROGRESS)).json()

    assert mine["rating"]["score"] == 7.3, "another rep's attempts leaked in"


# ── GET /me/profile ───────────────────────────────────────────────────────────


@pytest.mark.verifies("FR-TRP-004", "AC-TRP-004")
async def test_the_profiles_two_figures_come_from_different_pools(client, sign_in, world):
    """`total_completed_drills` counts self-authored practice; the rating does not.

    The rep has seven graded attempts — three on team drills and four private
    rehearsals. The gap between the two pools is now more than half the total,
    which is the point: a rating that quietly included private practice would be
    visibly wrong rather than plausibly wrong. Both numbers reading the same pool
    would err in one direction or the other:

    * a rating including the 2.0 rehearsal would let private practice drag down
      the figure a manager sees (AC-TRP-004);
    * a total excluding it would under-report the rep's own effort back to them,
      on a surface only they can see.

    The contract keeps the two apart by vocabulary — every manager-facing figure
    is a `counted_attempts`, and `total_completed_drills` exists in exactly one
    owner-only schema.
    """
    await sign_in()

    response = await client.get(PROFILE)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["account_id"] == str(world.rep)
    assert body["email"] == world.rep_email
    assert body["total_completed_drills"] == 7, "the rep's own practice was hidden from them"
    # Unchanged while the total nearly doubled — the two pools moving
    # independently is the assertion, not either number alone.
    assert body["lifetime_rating"] == {"score": 7.3, "band": "amber"}, (
        "private practice reached the rating"
    )


@pytest.mark.verifies("FR-TRP-005")
async def test_profile_returns_earned_and_locked_badges(client, sign_in):
    """Locked badges carry their goal.

    Returning only what has been earned would make the surface useless to the rep
    who has none yet — the profile is meant to show what is reachable.
    """
    await sign_in()

    badges = (await client.get(PROFILE)).json()["badges"]

    by_code = {b["code"]: b for b in badges}
    assert by_code["first_drill"]["earned"] is True
    assert by_code["first_drill"]["earned_at"] is not None
    assert by_code["ten_drills"]["earned"] is False
    assert by_code["ten_drills"]["earned_at"] is None
    assert by_code["ten_drills"]["rule_text"], "a locked badge with no goal shows nothing"
    assert badges[0]["earned"] is True, "earned badges sort first"


@pytest.mark.verifies("FR-TRP-005")
async def test_a_rep_sees_only_its_own_badges(client, sign_in, world):
    """Owner-only. `badge_award` has no manager policy, so a leaderboard cannot
    accidentally join to it — and this route offers no id to ask about."""
    await sign_in(world.other_rep_email)

    body = (await client.get(PROFILE)).json()

    assert body["account_id"] == str(world.other_rep)
    assert all(b["earned"] is False for b in body["badges"]), "another rep's award leaked"


# ── GET /me/coach-feedback ────────────────────────────────────────────────────


@pytest.mark.verifies("FR-TRP-003")
async def test_the_feed_is_newest_first_with_an_unread_count(client, sign_in, world):
    await sign_in()

    response = await client.get(FEEDBACK)

    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["id"] for item in body["data"]] == [
        str(world.feedback_new),
        str(world.feedback_old),
    ]
    assert body["unread_count"] == 2
    assert all(item["read"] is False for item in body["data"])


@pytest.mark.verifies("CMP-004")
async def test_the_feed_shows_only_the_callers_own_items(client, sign_in, world):
    """`other_rep` has an unread item of their own; it must not appear here, and
    must not inflate this reader's badge."""
    await sign_in()

    body = (await client.get(FEEDBACK)).json()

    assert str(world.other_feedback) not in [item["id"] for item in body["data"]]
    assert body["unread_count"] == 2, "another rep's unread item was counted"


@pytest.mark.verifies("FR-TRP-003")
async def test_the_feed_pages_by_cursor(client, sign_in, world):
    """Keyset, not offset.

    The feed grows at the head, so an offset page shifts under the reader between
    requests and silently repeats or skips an item.
    """
    await sign_in()

    first = (await client.get(FEEDBACK, params={"limit": 1})).json()
    assert [i["id"] for i in first["data"]] == [str(world.feedback_new)]
    assert first["pagination"]["has_more"] is True
    assert first["pagination"]["next_cursor"] is not None

    second = (
        await client.get(
            FEEDBACK, params={"limit": 1, "cursor": first["pagination"]["next_cursor"]}
        )
    ).json()

    assert [i["id"] for i in second["data"]] == [str(world.feedback_old)]
    assert second["pagination"]["has_more"] is False
    assert second["pagination"]["next_cursor"] is None


@pytest.mark.verifies("FR-TRP-003")
async def test_the_cursor_is_opaque(client, sign_in):
    """The contract types it `string` and calls it opaque, so it must not be a
    readable timestamp.

    A client that can read the cursor will eventually construct one, and then the
    ordering column is part of the public API and cannot be changed without
    breaking every caller.
    """
    await sign_in()

    cursor = (await client.get(FEEDBACK, params={"limit": 1})).json()["pagination"][
        "next_cursor"
    ]

    assert cursor is not None
    # Not a character check — base64's alphabet contains 'T', which is what the
    # first version of this test tripped over. The property is that a client
    # cannot READ it as a date: parsing must fail.
    with pytest.raises(ValueError):
        datetime.fromisoformat(cursor)


@pytest.mark.verifies("FR-TRP-003")
async def test_a_forged_cursor_is_refused(client, sign_in):
    """`422`, not a silent restart at the head of the feed.

    Treating an unreadable cursor as "no cursor" would send the reader back to the
    newest item — a pagination loop that looks like data rather than an error.
    """
    await sign_in()

    response = await client.get(FEEDBACK, params={"cursor": "not-a-real-cursor"})

    assert response.status_code == 422
    assert response.json()["type"].endswith("/validation-error")


@pytest.mark.verifies("FR-TRP-003")
async def test_the_limit_is_bounded(client, sign_in):
    """An unbounded page size is a client-supplied denial of service against
    ourselves, and the contract caps it at 100."""
    await sign_in()

    assert (await client.get(FEEDBACK, params={"limit": 101})).status_code == 422
    assert (await client.get(FEEDBACK, params={"limit": 0})).status_code == 422


# ── POST /me/coach-feedback/mark-read ─────────────────────────────────────────


@pytest.mark.verifies("FR-TRP-003")
async def test_marking_read_clears_the_item_and_everything_older(client, sign_in, world):
    await sign_in()

    response = await client.post(MARK_READ, json={"through_id": str(world.feedback_new)})

    assert response.status_code == 200, response.text
    assert response.json() == {"unread_count": 0}
    assert all(i["read"] for i in (await client.get(FEEDBACK)).json()["data"])


@pytest.mark.verifies("FR-TRP-003")
async def test_marking_through_the_older_item_leaves_the_newer_unread(
    client, sign_in, world
):
    """Through-an-id means "and everything older", not "everything"."""
    await sign_in()

    response = await client.post(MARK_READ, json={"through_id": str(world.feedback_old)})

    assert response.json() == {"unread_count": 1}
    by_id = {i["id"]: i for i in (await client.get(FEEDBACK)).json()["data"]}
    assert by_id[str(world.feedback_old)]["read"] is True
    assert by_id[str(world.feedback_new)]["read"] is False


@pytest.mark.verifies("FR-TRP-003")
async def test_replaying_mark_read_is_a_no_op_success(client, sign_in, world):
    """An update that changes nothing because everything was already read is an
    ordinary replay, not a failure."""
    await sign_in()
    first = await client.post(MARK_READ, json={"through_id": str(world.feedback_new)})
    second = await client.post(MARK_READ, json={"through_id": str(world.feedback_new)})

    assert first.status_code == second.status_code == 200
    assert second.json() == {"unread_count": 0}


@pytest.mark.verifies("AC-IDA-006")
async def test_another_reps_item_is_not_found_and_marks_nothing(client, sign_in, world):
    """The cross-reader case, and the one that fails silently.

    An id belonging to somebody else must not resolve to THEIR timestamp and mark
    this reader's feed read against a stranger's clock. RLS would not catch that —
    every row written would still belong to the caller — so the guard is the
    `rep_account_id` predicate inside the subquery, and this is what holds it
    there.

    `404`, not `403`: distinguishing "not yours" from "does not exist" would turn
    the endpoint into a probe for which feedback ids exist (ADR-0036).
    """
    await sign_in()

    response = await client.post(MARK_READ, json={"through_id": str(world.other_feedback)})

    assert response.status_code == 404
    assert response.json()["type"].endswith("/not-found")
    assert (await client.get(FEEDBACK)).json()["unread_count"] == 2, "the feed was marked read"


@pytest.mark.verifies("AC-IDA-006")
async def test_an_unknown_id_is_not_found(client, sign_in):
    await sign_in()

    response = await client.post(
        MARK_READ, json={"through_id": "00000000-0000-0000-0000-000000000000"}
    )

    assert response.status_code == 404


# ── the surface is behind the gate ───────────────────────────────────────────


@pytest.mark.verifies("SEC-002")
@pytest.mark.parametrize("path", [PROGRESS, PROFILE, FEEDBACK])
async def test_every_read_requires_a_session(client, path):
    response = await client.get(path)

    assert response.status_code == 401
    assert response.json()["type"].endswith("/session-invalid")


@pytest.mark.verifies("SEC-002")
async def test_mark_read_requires_a_session(client, world):
    response = await client.post(MARK_READ, json={"through_id": str(world.feedback_new)})

    assert response.status_code == 401
