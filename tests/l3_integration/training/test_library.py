"""The drill library — `GET /me/library` (FR-TRP-006..013).

Assertions come from the contract and the FRs, not from reading the query. The
query was written first and verified by hand against the database, so a suite
derived from it would only prove it does what it does.

The property this suite exists for is **the universe**. A library is defined by
what it leaves out as much as by what it holds, and four of the six seeded drills
are there to be excluded:

    manager_draft    unpublished, not the caller's — belongs to GET /team/drills
    archived_drill   withdrawn (FR-DRL-016), invisible to everyone
    the other rep's allowance on a shared assignment
    another rep's attempt on a drill this rep has not tried

Each of those is a row that a plausible simplification of the query would let
through. The count assertions are deliberately exact for that reason: `>= 1` would
pass while the grid quietly leaked somebody's unpublished work.
"""

from __future__ import annotations

from base64 import urlsafe_b64encode

import pytest

pytestmark = [
    pytest.mark.l3_integration,
    pytest.mark.l7_security,
    pytest.mark.invariant_path,
]

LIBRARY = "/api/v1/me/library"


def _by_id(body: dict) -> dict:
    """Index the page by drill, asserting one card per drill on the way through.

    The assert is not defensive padding. Keying a dict by id silently keeps the
    LAST of any duplicate pair, so a join that fans out — the allowance lateral
    losing its account predicate and matching every recipient of the assignment,
    say — produces two cards and this helper hides one of them. A mutation that
    did exactly that passed six assertions before this line existed.
    """
    ids = [card["drill_id"] for card in body["data"]]
    assert len(ids) == len(set(ids)), f"a drill appears more than once: {ids}"
    return {card["drill_id"]: card for card in body["data"]}


# ── the universe ──────────────────────────────────────────────────────────────


@pytest.mark.verifies("FR-TRP-006")
async def test_the_grid_holds_every_drill_the_rep_can_practise_and_nothing_else(
    client, sign_in, world
):
    """Assigned, team-published, and the rep's own — plus their own draft.

    Exactly four. The manager's draft and the archived drill are seeded into the
    same team and must not appear: one is unpublished work belonging to another
    author (FR-DRL-013), the other is withdrawn (FR-DRL-016).
    """
    await sign_in()

    body = (await client.get(LIBRARY)).json()
    cards = _by_id(body)

    assert set(cards) == {
        str(world.drill_discovery),
        str(world.drill_renewal),
        str(world.self_drill),
        str(world.self_draft),
    }


# Untagged, and FR-DRL-013 is the tag it does not carry. That requirement has six
# clauses and this endpoint answers three of them: drafts are listed only in their
# author's surface, marked `status=draft`, and no other principal sees them. The
# rest — resuming one via `GET /drills/{id}`, manager drafts appearing in
# `GET /team/drills`, and a draft refusing to start with `409 drill-not-startable`
# — need operations that do not exist yet. Tagging it would close a ratchet entry
# and record the unbuilt half as verified.
async def test_the_reps_own_draft_appears_marked_and_unnamed(client, sign_in, world):
    """A draft carries `status="draft"` and a null label.

    The persona that names a drill is generated at publish (FR-DRL-004), so an
    unpublished one has no name yet — the card has to say so rather than invent
    one or omit the key.
    """
    await sign_in()

    card = _by_id((await client.get(LIBRARY)).json())[str(world.self_draft)]

    assert card["status"] == "draft"
    assert card["label"] is None
    assert card["source"] == "self_authored"
    assert card["best"] is None


@pytest.mark.verifies("FR-TRP-006")
async def test_a_published_card_carries_its_best_score_and_band(client, sign_in, world):
    """Discovery was attempted twice, 7.6 and 8.4. Best is 8.4, which bands green.

    Not the latest and not the mean — FR-TRP-006 says best, and the three differ
    here on purpose.
    """
    await sign_in()

    card = _by_id((await client.get(LIBRARY)).json())[str(world.drill_discovery)]

    assert card["attempted"] is True
    assert card["best"] == {"score": 8.4, "band": "green"}


# ── the assignment block (FR-TRP-013) ─────────────────────────────────────────


@pytest.mark.verifies("FR-TRP-006", "FR-TRP-013")
async def test_an_assigned_card_carries_its_due_date_and_allowance(client, sign_in, world):
    await sign_in()

    card = _by_id((await client.get(LIBRARY)).json())[str(world.drill_renewal)]

    assert card["source"] == "assigned"
    assert card["assignment"]["attempts_used"] == 1
    assert card["assignment"]["attempts_allowed"] == 3
    assert card["assignment"]["locked"] is False
    assert card["assignment"]["due_date"]


@pytest.mark.verifies("FR-TRP-013", "AC-TRP-005")
async def test_an_exhausted_allowance_reads_locked_for_this_rep_alone(client, sign_in, world):
    """3 of 3 used, and the OTHER rep holds the same assignment at 0 of 3.

    That second row is the whole point of the test. Both recipients hang off one
    `assignment`, so a query joining it without `rep_account_id = :account` reads
    a counter that belongs to someone else — and reports an exhausted drill as
    available, which is the one state FR-TRP-013 exists to prevent.
    """
    await sign_in()

    body = (await client.get(LIBRARY)).json()
    card = _by_id(body)[str(world.drill_discovery)]

    # Counted, not just looked up: without the account predicate the lateral
    # matches both recipients and this drill comes back twice.
    matching = [c for c in body["data"] if c["drill_id"] == str(world.drill_discovery)]
    assert len(matching) == 1, f"one card per assigned drill, got {len(matching)}"
    assert card["assignment"]["attempts_used"] == 3
    assert card["assignment"]["locked"] is True


@pytest.mark.verifies("FR-TRP-006")
async def test_an_unassigned_card_carries_no_assignment_block(client, sign_in, world):
    await sign_in()

    card = _by_id((await client.get(LIBRARY)).json())[str(world.self_drill)]

    assert card["source"] == "self_authored"
    assert card["assignment"] is None
    # Best of 4.0/9.0/5.0/2.0. Asserted because FR-TRP-010 excludes self-authored
    # practice from ratings but NOT from its own drill's figures — so this card
    # must carry a score even though none of those attempts moved the rep's
    # rating. A card reading the counted pool would show null here.
    assert card["best"] == {"score": 9.0, "band": "green"}


# ── tabs and refinements (FR-TRP-007/008) ─────────────────────────────────────


@pytest.mark.verifies("FR-TRP-007")
async def test_counts_describe_every_tab_not_just_the_active_one(client, sign_in):
    """Counts are what each tab WOULD show, so they cannot move with the tab.

    A window function over the final select computes the opposite — `WHERE` runs
    before window functions, so every tab would report its own size and the other
    two would read zero. Asserting the counts are identical across all three
    requests is what catches that.
    """
    await sign_in()

    expected = {"all": 4, "assigned": 2, "self_authored": 2}
    for tab in ("all", "assigned", "self_authored"):
        body = (await client.get(LIBRARY, params={"source": tab})).json()
        assert body["counts"] == expected, f"counts moved on source={tab}"


@pytest.mark.verifies("FR-TRP-007")
async def test_a_tab_narrows_the_rows_it_returns(client, sign_in, world):
    await sign_in()

    assigned = (await client.get(LIBRARY, params={"source": "assigned"})).json()
    authored = (await client.get(LIBRARY, params={"source": "self_authored"})).json()

    assert set(_by_id(assigned)) == {str(world.drill_discovery), str(world.drill_renewal)}
    assert set(_by_id(authored)) == {str(world.self_drill), str(world.self_draft)}


@pytest.mark.verifies("FR-TRP-008")
async def test_the_call_type_filter_narrows_rows_and_counts_together(client, sign_in, world):
    """Counts follow the refinement, unlike the tab.

    A tab count that ignored an active call-type filter would read 4 while showing
    1 — the mismatch a reader interprets as missing data.
    """
    await sign_in()

    body = (await client.get(LIBRARY, params={"call_type": "renewal"})).json()

    assert set(_by_id(body)) == {str(world.drill_renewal)}
    assert body["counts"] == {"all": 1, "assigned": 1, "self_authored": 0}


@pytest.mark.verifies("FR-TRP-007")
async def test_an_empty_page_still_reports_the_counts(client, sign_in):
    """Zero rows, non-zero counts — and this is the case that breaks first.

    Only the draft is an upsell, and it is self-authored, so filtering to
    `source=assigned` on top of it yields nothing. The counts must still say the
    self-authored tab holds one, because that is the reader's only signal that
    their content is somewhere other than where they are looking.

    The obvious way to write this query — computing counts as a `CROSS JOIN` onto
    the page — returns NO rows at all when the page is empty, so the response
    cannot be built. An empty grid is exactly when a rep needs the other tabs'
    numbers, so this is not a rare path.
    """
    await sign_in()

    body = (
        await client.get(LIBRARY, params={"call_type": "upsell", "source": "assigned"})
    ).json()

    assert body["data"] == []
    assert body["counts"] == {"all": 1, "assigned": 0, "self_authored": 1}
    assert body["pagination"] == {"next_cursor": None, "has_more": False}


@pytest.mark.verifies("FR-TRP-008")
async def test_the_attempted_filter_is_the_mirror_of_unattempted(client, sign_in, world):
    """The two halves must partition the grid.

    Asserted as a partition rather than as a list, because a filter that dropped
    rows from both sides would still return a plausible-looking subset on each.
    """
    await sign_in()

    attempted = (await client.get(LIBRARY, params={"attempted": "attempted"})).json()
    unattempted = (await client.get(LIBRARY, params={"attempted": "unattempted"})).json()
    everything = (await client.get(LIBRARY)).json()

    assert set(_by_id(attempted)) == {
        str(world.drill_discovery),
        str(world.drill_renewal),
        str(world.self_drill),
    }
    assert set(_by_id(attempted)) | set(_by_id(unattempted)) == set(_by_id(everything))
    assert not set(_by_id(attempted)) & set(_by_id(unattempted))


@pytest.mark.verifies("FR-TRP-008")
async def test_refinements_combine_rather_than_override(client, sign_in, world):
    """Two filters at once narrow to their intersection.

    Discovery holds two drills, one of them self-authored; both are attempted. So
    call_type alone gives two and attempted alone gives three, and together they
    must give exactly the overlap — a query applying only the last-read parameter
    would return one of the wider sets and look correct in isolation.
    """
    await sign_in()

    body = (
        await client.get(LIBRARY, params={"call_type": "discovery", "attempted": "attempted"})
    ).json()

    assert set(_by_id(body)) == {str(world.drill_discovery), str(world.self_drill)}
    assert body["counts"] == {"all": 2, "assigned": 1, "self_authored": 1}


@pytest.mark.verifies("FR-TRP-008")
async def test_the_unattempted_filter_finds_the_drill_no_one_here_has_tried(
    client, sign_in, world
):
    """Only the draft is unattempted for this rep.

    `attempted` must mean "by me". The other rep's graded attempts sit on the same
    published drills, so a filter reading the table without an account predicate
    would call them attempted and return nothing.
    """
    await sign_in()

    body = (await client.get(LIBRARY, params={"attempted": "unattempted"})).json()

    assert set(_by_id(body)) == {str(world.self_draft)}


# ── ordering and pagination (FR-TRP-008) ──────────────────────────────────────


@pytest.mark.verifies("FR-TRP-008")
async def test_recommended_leads_with_the_soonest_due_assignment(client, sign_in, world):
    """Assigned first, by due date ascending — renewal (+2 days) before discovery
    (+9). Then the rest, with the unstartable draft last."""
    await sign_in()

    order = [card["drill_id"] for card in (await client.get(LIBRARY)).json()["data"]]

    assert order[0] == str(world.drill_renewal)
    assert order[1] == str(world.drill_discovery)
    assert order[-1] == str(world.self_draft)


@pytest.mark.verifies("FR-TRP-008")
async def test_recently_assigned_orders_by_grant_not_by_due_date(client, sign_in, world):
    """The second ordering, and it must disagree with the first.

    Renewal is due sooner but was granted ten days ago; discovery is due later and
    was granted yesterday. So `recommended` leads with renewal and this one leads
    with discovery. A sort reading the wrong column passes under a fixture where
    one drill wins both — which is why the seeded dates are crossed.
    """
    await sign_in()

    order = [
        card["drill_id"]
        for card in (
            await client.get(LIBRARY, params={"sort": "recently_assigned"})
        ).json()["data"]
    ]

    assert order[0] == str(world.drill_discovery)
    assert order[1] == str(world.drill_renewal)


@pytest.mark.verifies("FR-TRP-008")
async def test_paging_covers_the_grid_once_with_no_repeats(client, sign_in):
    """Walk the whole grid two at a time and reassemble it.

    A keyset on a non-unique key repeats a row or drops one at the page boundary,
    and both look like a rendering bug rather than a query defect. Comparing the
    walked sequence against the single-page one is what makes that visible.
    """
    await sign_in()

    whole = [c["drill_id"] for c in (await client.get(LIBRARY)).json()["data"]]

    walked: list[str] = []
    cursor: str | None = None
    for _ in range(10):  # bounded: a cursor that never advances must not hang here
        params = {"limit": 2} | ({"cursor": cursor} if cursor else {})
        body = (await client.get(LIBRARY, params=params)).json()
        walked.extend(c["drill_id"] for c in body["data"])
        cursor = body["pagination"]["next_cursor"]
        if not cursor:
            break

    assert walked == whole
    assert len(walked) == len(set(walked))


@pytest.mark.verifies("FR-TRP-008")
async def test_the_last_page_offers_no_cursor(client, sign_in):
    await sign_in()

    body = (await client.get(LIBRARY, params={"limit": 100})).json()

    assert body["pagination"]["has_more"] is False
    assert body["pagination"]["next_cursor"] is None


async def test_a_cursor_from_a_different_ordering_is_refused(client, sign_in):
    """Bucket numbers mean different things under each sort.

    Read under the wrong one the reader lands somewhere arbitrary — rows skipped,
    no error, looks like missing data. A `422` is the only honest answer.
    """
    await sign_in()

    first = (await client.get(LIBRARY, params={"limit": 1, "sort": "recommended"})).json()
    cursor = first["pagination"]["next_cursor"]

    response = await client.get(
        LIBRARY, params={"limit": 1, "sort": "recently_assigned", "cursor": cursor}
    )

    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    ("bucket", "sort_key"),
    [
        ("99999999999999999999", "0"),   # beyond int4 — asyncpg raises, not the app
        ("-99999999999999999999", "0"),
        ("4", "0"),                      # a group no ordering emits
        ("-1", "0"),
        ("0", "nan"),                    # parses as Decimal, compares as nothing
        ("0", "inf"),
        ("0", "-inf"),
    ],
)
async def test_a_structurally_valid_but_impossible_cursor_is_refused(
    client, sign_in, bucket, sort_key
):
    """Decodable, and still not one this server could have minted.

    These get past the base64 and past `int()`/`Decimal()` — the parts parse. The
    bug they found is that parsing is not validation: an out-of-int4 bucket
    reached the driver and came back as an unhandled `DataError` from inside query
    execution, which is a `500` for a request that should never have got that far.
    NaN and Infinity parse too, and then compare as nothing meaningful.

    A `500` here is worse than the wrong rows: it is an unhandled path reachable
    by anyone who can edit a query string.
    """
    await sign_in()
    raw = f"recommended|{bucket}|{sort_key}|00000000-0000-7000-8000-000000000000"
    cursor = urlsafe_b64encode(raw.encode()).decode()

    response = await client.get(LIBRARY, params={"cursor": cursor})

    assert response.status_code == 422, response.text


async def test_a_cursor_this_server_did_not_mint_is_refused(client, sign_in):
    """Not silently restarted at the head — that is a pagination loop that reads
    as data rather than as an error."""
    await sign_in()

    response = await client.get(LIBRARY, params={"cursor": "not-a-real-cursor"})

    assert response.status_code == 422, response.text


async def test_an_out_of_range_limit_is_refused_before_a_query_runs(client, sign_in):
    """An unbounded page size is a client-supplied denial of service against
    ourselves; FastAPI rejects it from the signature."""
    await sign_in()

    assert (await client.get(LIBRARY, params={"limit": 0})).status_code == 422
    assert (await client.get(LIBRARY, params={"limit": 101})).status_code == 422
    # Both ends of the accepted range, so the bound is asserted as a range rather
    # than as two rejections that a stricter-than-intended limit would also pass.
    assert (await client.get(LIBRARY, params={"limit": 1})).status_code == 200
    assert (await client.get(LIBRARY, params={"limit": 100})).status_code == 200


# ── concealment ───────────────────────────────────────────────────────────────


@pytest.mark.verifies("AC-TRP-004")
async def test_a_manager_sees_no_reps_private_work_and_no_drafts(client, sign_in, world):
    """`/me/library` is not role-gated, so a manager can call it — and RLS WIDENS
    for them: P3 returns every team draft.

    So the query's own predicates are what hold here, not the policies. A manager
    gets the published team drills and nothing else: not the rep's self-authored
    drill (AC-TRP-004), not the rep's draft, and not their own draft either, which
    belongs to `GET /team/drills` (FR-DRL-013).
    """
    await sign_in(world.manager_email)

    body = (await client.get(LIBRARY)).json()

    assert set(_by_id(body)) == {str(world.drill_discovery), str(world.drill_renewal)}
    assert body["counts"] == {"all": 2, "assigned": 0, "self_authored": 0}


@pytest.mark.verifies("AC-TRP-004")
async def test_a_teammate_never_sees_another_reps_self_authored_drill(client, sign_in, world):
    await sign_in(world.other_rep_email)

    cards = _by_id((await client.get(LIBRARY)).json())

    assert str(world.self_drill) not in cards
    assert str(world.self_draft) not in cards


async def test_the_library_requires_a_session(client):
    assert (await client.get(LIBRARY)).status_code == 401
