"""`GET /team/drills` — the team's drills (FR-TRM-008).

Driven from `world` rather than `team_world`, because `world` already holds the
exact mix this endpoint has to sort out: two published team drills, one draft, one
archived, and TWO self-authored rep drills that must never appear at all.

## The concealment rule is the point of this surface

FR-TRP-009 makes a rep's self-authored drill private to its author — "no other
rep, and not the manager, can see the drill, its attempts, or their reviews
anywhere". The catalog is where that is most easily broken, because it is the one
place a manager legitimately asks for *all* the team's drills.

Two of `world`'s six drills are self-authored, one published and one still a
draft, so an implementation that filtered only on status, or forgot the predicate
entirely, produces a visibly longer list rather than a subtly wrong one.

## Ordering is asserted as a property, not a sequence

Every drill in `world` is created in one transaction, so they share an
`updated_at` to the microsecond. That is realistic — a bulk import or a migration
would do the same — and it is why the cursor cannot be a bare timestamp. The tests
below assert that paging is complete and duplicate-free rather than pinning a
literal order that would encode UUID minting sequence into an expectation.
Verified by reducing the keyset to the timestamp alone, which fails exactly the
paging test.

## One clause of FR-TRM-008 rests on an affordance

It ends "opening a published drill leads to its detail (FR-TRM-009)", and
`GET /team/drills/{drill_id}/stats` does not exist yet. What the catalog owes that
clause is `drill_id`, which every row carries and every test above reads. Whether
it resolves cannot be shown until the detail does — the same position FR-TRM-005
was in last task, and that debt was paid the moment the deep dive landed. The
equivalent test belongs with the drill stats endpoint.
"""

from __future__ import annotations

from datetime import datetime

import pytest

pytestmark = [pytest.mark.l3_integration, pytest.mark.l7_security]

CATALOG = "/api/v1/team/drills"


async def _all_items(client, **params) -> list[dict]:
    response = await client.get(CATALOG, params=params)
    assert response.status_code == 200, response.text
    return response.json()["data"]


# ── What the catalog contains ─────────────────────────────────────────────────


@pytest.mark.verifies("FR-TRM-008")
async def test_the_catalog_lists_the_teams_drills_across_every_status(
    client, sign_in, world
):
    """Published, draft and archived together — the default view is `all`.

    A catalog that showed only published drills would hide the work in progress,
    which is the half a manager is most likely to be looking for.
    """
    await sign_in(world.manager_email)

    items = await _all_items(client)

    assert {i["drill_id"] for i in items} == {
        str(world.drill_discovery),
        str(world.drill_renewal),
        str(world.manager_draft),
        str(world.archived_drill),
    }
    assert {i["status"] for i in items} == {"published", "draft", "archived"}


@pytest.mark.verifies("FR-TRP-009", "AC-TRP-004")
async def test_self_authored_rep_drills_never_appear(client, sign_in, world):
    """The rule this surface exists to not break.

    `world` seeds two: one published, one still a draft. Both belong to a rep, and
    FR-TRP-009 keeps them private to their author — from every other rep AND from
    the manager. Asserting both catches an implementation that excluded
    self-authored drills only once they were published.
    """
    await sign_in(world.manager_email)

    ids = {i["drill_id"] for i in await _all_items(client)}

    assert str(world.self_drill) not in ids
    assert str(world.self_draft) not in ids


@pytest.mark.verifies("FR-TRP-009")
async def test_a_status_filter_does_not_reopen_the_self_authored_drills(
    client, sign_in, world
):
    """The published self-authored drill is published; the draft one is a draft.

    So a filter applied without the concealment predicate lets each back in
    through its own status — which is exactly how a privacy rule survives the
    default view and fails on a tab.
    """
    await sign_in(world.manager_email)

    published = {i["drill_id"] for i in await _all_items(client, status="published")}
    drafts = {i["drill_id"] for i in await _all_items(client, status="draft")}

    assert str(world.self_drill) not in published
    assert str(world.self_draft) not in drafts


# ── Each row's numbers ────────────────────────────────────────────────────────


@pytest.mark.verifies("FR-TRM-008")
async def test_each_drill_carries_its_rollup(client, sign_in, world):
    """Average score and attempt count, from V-7.

    Discovery holds three graded attempts — 7.6 and 8.4 from one rep, 9.9 from the
    other — averaging 8.6333, which rounds to 8.6. Both reps count: this is the
    DRILL's rollup, not one rep's history.
    """
    await sign_in(world.manager_email)

    items = {i["drill_id"]: i for i in await _all_items(client)}
    discovery = items[str(world.drill_discovery)]
    renewal = items[str(world.drill_renewal)]

    assert discovery["average_score"] == {"score": 8.6, "band": "green"}
    assert discovery["total_attempts"] == 3
    assert renewal["average_score"] == {"score": 6.0, "band": "amber"}
    assert renewal["total_attempts"] == 1


@pytest.mark.verifies("FR-TRM-008")
async def test_a_drill_nobody_has_attempted_is_unrated_not_zero(client, sign_in, world):
    """`fn_score_band(null)` returns `'red'`, not null.

    `null >= 7.5` is null and falls through to the else, so an unguarded read
    reports a drill nobody has taken as a red one — the worst possible reading of
    "no data", on a row a manager might be deciding whether to publish.
    """
    await sign_in(world.manager_email)

    items = {i["drill_id"]: i for i in await _all_items(client)}

    assert items[str(world.manager_draft)]["average_score"] is None
    assert items[str(world.manager_draft)]["total_attempts"] == 0
    assert items[str(world.archived_drill)]["average_score"] is None


@pytest.mark.verifies("FR-TRM-008")
async def test_a_draft_without_a_generated_scenario_has_a_null_label(
    client, sign_in, world
):
    """The contract types `label` nullable for exactly this row.

    A draft carries no frozen content until it is published, so it has no
    persona-derived label yet. Returning an empty string instead would be a
    different claim — that it has a name, and the name is blank.
    """
    await sign_in(world.manager_email)

    items = {i["drill_id"]: i for i in await _all_items(client)}

    assert items[str(world.manager_draft)]["label"] is None
    assert items[str(world.drill_discovery)]["label"] == "discovery drill"


@pytest.mark.verifies("FR-TRM-008")
async def test_every_row_carries_its_last_update_and_they_descend(client, sign_in, world):
    """"Last update" is a clause of FR-TRM-008, and the sort key besides.

    Asserted as presence plus a non-increasing sequence rather than as literals:
    `world` builds its drills in one transaction, so their timestamps are equal to
    the microsecond and any exact expectation would be pinning the clock rather
    than the behaviour. Non-increasing still fails an ascending sort, an absent
    field, or a null one.
    """
    await sign_in(world.manager_email)

    items = await _all_items(client)
    stamps = [datetime.fromisoformat(i["updated_at"]) for i in items]

    assert len(stamps) == 4
    assert stamps == sorted(stamps, reverse=True)


# ── The status filter ─────────────────────────────────────────────────────────


@pytest.mark.verifies("FR-TRM-008")
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("published", {"discovery", "renewal"}),
        ("draft", {"manager_draft"}),
        ("archived", {"archived"}),
    ],
)
async def test_the_status_filter_narrows_to_one_kind(
    client, sign_in, world, status, expected
):
    await sign_in(world.manager_email)

    by_key = {
        "discovery": str(world.drill_discovery),
        "renewal": str(world.drill_renewal),
        "manager_draft": str(world.manager_draft),
        "archived": str(world.archived_drill),
    }

    items = await _all_items(client, status=status)

    assert {i["drill_id"] for i in items} == {by_key[k] for k in expected}
    assert {i["status"] for i in items} == {status}


# ── Pagination ────────────────────────────────────────────────────────────────


@pytest.mark.verifies("FR-TRM-008")
async def test_paging_covers_every_drill_exactly_once(client, sign_in, world):
    """The property that matters, rather than a literal page order.

    All four drills are created in one transaction and share an `updated_at` to
    the microsecond, so a cursor keyed on the timestamp alone either repeats rows
    or skips them. Walking the pages and comparing against the unpaged result
    catches both, and does not depend on the order UUIDs happened to be minted in.
    """
    await sign_in(world.manager_email)

    unpaged = [i["drill_id"] for i in await _all_items(client)]

    walked: list[str] = []
    cursor: str | None = None
    for _ in range(10):  # generous bound; four drills at two per page needs two
        params = {"limit": 2} | ({"cursor": cursor} if cursor else {})
        body = (await client.get(CATALOG, params=params)).json()
        walked.extend(i["drill_id"] for i in body["data"])
        cursor = body["pagination"]["next_cursor"]
        if not body["pagination"]["has_more"]:
            break

    assert cursor is None, "the last page still offered a cursor"
    assert walked == unpaged
    assert len(set(walked)) == len(walked), "a drill appeared on two pages"


async def test_a_cursor_from_a_different_status_filter_is_refused(client, sign_in, world):
    """A cursor is a position inside ONE result set.

    Carried across a filter change it still decodes and the keyset still applies,
    so the response looks like a perfectly good page — while every published drill
    that sorted ahead of the cursor is silently missing. The client is never told
    it lost rows, which is precisely why this is a refusal rather than a
    best-effort answer.

    `_LibraryCursor` settled the same question for its sort orders; this follows
    it rather than inventing a second policy for the same defect.
    """
    await sign_in(world.manager_email)

    first = (await client.get(CATALOG, params={"limit": 1})).json()
    cursor = first["pagination"]["next_cursor"]
    assert cursor, "the fixture must have more than one drill for this to mean anything"

    response = await client.get(CATALOG, params={"status": "published", "cursor": cursor})

    assert response.status_code == 422


@pytest.mark.parametrize("limit", [0, 101])
async def test_a_page_size_outside_the_contract_is_refused(client, sign_in, world, limit):
    """Bounded in the signature, so FastAPI answers before a query is built.

    An unbounded `limit` is a client-supplied denial of service against ourselves;
    the contract's 1..100 is the answer and this pins both ends of it.
    """
    await sign_in(world.manager_email)

    response = await client.get(CATALOG, params={"limit": limit})

    assert response.status_code == 422


async def test_a_cursor_this_server_did_not_mint_is_refused(client, sign_in, world):
    """`422`, not a 500 and not a silently-ignored parameter.

    A cursor is opaque, so a client cannot construct a valid one — which means any
    cursor that fails to decode came from somewhere else, and quietly serving page
    one would hide that from whoever sent it.
    """
    await sign_in(world.manager_email)

    response = await client.get(CATALOG, params={"cursor": "not-a-real-cursor"})

    assert response.status_code == 422


# ── Scope and the gate ────────────────────────────────────────────────────────


@pytest.mark.verifies("AC-TRM-006")
async def test_another_teams_drills_are_absent(client, sign_in, team_world):
    """AC-TRM-006's catalog clause — the third of its three surfaces.

    `team_world` holds a second manager in the SAME org with a drill of their own,
    and `other_drill` is the only id that can demonstrate this. An earlier version
    of this test asserted the self-authored drill's absence instead — which is the
    concealment rule, covered twice already above, and says nothing about teams.
    It passed, and it was named for something it did not check.

    Both refusals are enforced by one RLS policy: `app_drill_manageable_by_team`
    is `d.team_id = app.team_id and not d.self_authored`. That they share a
    mechanism is exactly why they need separate tests — a change to that function
    could relax either half alone.
    """
    await sign_in(team_world.manager_email)

    ids = {i["drill_id"] for i in await _all_items(client)}

    assert str(team_world.drill_discovery) in ids
    assert str(team_world.other_drill) not in ids, "another team's drill was listed"


@pytest.mark.parametrize(
    "cursor",
    [
        "not-base64-at-all!!",
        "",
        "YWJj",  # valid base64, no separators
        "YWxsfG5vdC1hLWRhdGV8bm90LWEtdXVpZA==",  # all|not-a-date|not-a-uuid
        "YWxsfDIwMjYtMDgtMDFUMDA6MDA6MDArMDA6MDA=",  # two fields, not three
        "YWxsfDIwMjYtMDgtMDFUMDA6MDA6MDArMDA6MDB8YXxifGM=",  # too many fields
        "//////8=",  # valid base64, invalid utf-8
    ],
)
async def test_a_malformed_cursor_is_refused_rather_than_crashing(
    client, sign_in, world, cursor
):
    """Parsing is not validation, and the difference is a 500.

    The cursor is the only structured value on this surface a caller composes
    themselves — a path parameter gets UUID-checked by FastAPI, but this arrives
    as an opaque string and is split, base64-decoded and parsed into three typed
    fields. Every step is a chance to raise something the handler never catches.

    Each case below targets a different one: the base64 layer, the UTF-8 decode,
    the field count, and each field's own parser. All must answer `422` — a `500`
    would be an unhandled exception reachable from a query string.
    """
    await sign_in(world.manager_email)

    response = await client.get(CATALOG, params={"cursor": cursor})

    assert response.status_code == 422, f"{cursor!r} produced {response.status_code}"


async def test_a_rep_cannot_reach_the_catalog(client, sign_in, world):
    """The role gate. A rep browses drills through `/me/library`, which shows the
    team's PUBLISHED drills and their own — never the team's drafts."""
    await sign_in(world.rep_email)

    response = await client.get(CATALOG)

    assert response.status_code == 404
