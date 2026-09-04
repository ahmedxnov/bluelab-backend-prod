"""The three denials that are structural rather than projection-dependent.

Each of these could have been implemented as an API filter. Each is instead the
*absence of a policy*, so there is no row for a serialisation bug to leak — and
that is precisely what these tests assert. A passing projection test proves the
API filters correctly today; these prove the database would not hand the rows
over even if it stopped.

    AC-TRP-004   a rep's self-authored drill is invisible to their manager
    FR-SCR-017   the concealed set is lifted by AUTHORSHIP, never by publication
    FR-SCR-018   candidates see no evaluation, ever
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

pytestmark = [pytest.mark.l3_integration, pytest.mark.invariant_path]


async def _count(session, relation: str, **params: object) -> int:
    where = " and ".join(f"{k} = :{k}" for k in params) or "true"
    return (
        await session.execute(text(f"select count(*) from {relation} where {where}"), params)
    ).scalar_one()


# ── AC-TRP-004 · self-authored privacy ───────────────────────────────────────

@pytest.mark.verifies("AC-TRP-004", "FR-TRP-009")
async def test_manager_cannot_see_own_reps_self_authored_drill(as_principal, manager, world):
    """M1 manages R1a and still cannot see R1a's private practice drill.

    The sharpest case in the whole policy set: the manager owns the team, owns
    the drill's `team_id`, and is denied anyway. Coaching, not surveillance —
    as a database property rather than a UI rule (P-1).
    """
    async with as_principal(manager(world.m1, world.org_a)) as session:
        assert await _count(session, "drill", id=world.self_authored_drill) == 0


@pytest.mark.verifies("AC-TRP-004")
async def test_author_can_see_their_own_self_authored_drill(as_principal, rep, world):
    """The mirror. A denial test that never proves the positive case is a test
    that would still pass if the table were simply empty."""
    async with as_principal(rep(world.r1a, world.m1, world.org_a)) as session:
        assert await _count(session, "drill", id=world.self_authored_drill) == 1


@pytest.mark.verifies("AC-TRP-004")
async def test_teammate_cannot_see_another_reps_self_authored_drill(as_principal, rep, world):
    async with as_principal(rep(world.r1b, world.m1, world.org_a)) as session:
        assert await _count(session, "drill", id=world.self_authored_drill) == 0


# ── assignment is read by MEMBERSHIP, never by team ──────────────────────────
#
# `assignment` is otherwise a manager-only table (P3). The rep's read exists
# because the library card needs `due_date` and `attempts_allowed`, which live
# here and nowhere else — `assignment_recipient` carries only `attempts_used`.
#
# The obvious way to grant that read is `team_id = app.team_id`, and it is wrong:
# it would hand every rep on the team every assignment made to any of their peers.
# The tests below are what stands between the narrow grant and that shortcut, so
# they assert the shape of the predicate, not just that a row comes back.
#
# NO `verifies` TAGS BELOW, deliberately. The property is peer privacy, and it has
# no requirement id to cite: 20-training-rep §"NFR & compliance" states the
# coaching-not-surveillance stance is "enforced by composition — no requirement in
# this file surfaces another rep's identity or scores", and P-1 lives in ux/00, not
# in a citable family.
#
# The near misses were checked and rejected rather than assumed. AC-TRP-005 and
# FR-TRP-013 are about an EXHAUSTED allowance rendering locked at 3/3; nothing here
# exhausts one. FR-TRM-012 is the manager's re-assignment granting a fresh
# allowance, which is unimplemented — tagging it would have closed a ratchet
# baseline entry and recorded unbuilt work as verified. An untagged test still
# fails when the policy widens; a mistagged one corrupts the traceability the
# ratchet exists to protect.


async def test_recipient_reads_the_assignment_they_hold(as_principal, rep, world):
    """The positive case. Without it the denials below would pass on an empty table."""
    async with as_principal(rep(world.r1a, world.m1, world.org_a)) as session:
        assert await _count(session, "assignment", id=world.assignment) == 1


async def test_teammate_cannot_read_an_assignment_they_did_not_receive(
    as_principal, rep, world
):
    """R1b shares R1a's org, team and manager, and is denied.

    Everything a team predicate could match on is identical between these two
    reps; the only difference is the `assignment_recipient` row. So this fails the
    moment the grant is widened to the team — which is the mistake it exists to
    catch. A peer's due date and allowance are practice volume, and P-1 forbids
    surfacing that between reps.
    """
    async with as_principal(rep(world.r1b, world.m1, world.org_a)) as session:
        assert await _count(session, "assignment", id=world.assignment) == 0


async def test_teammate_cannot_read_another_reps_allowance(as_principal, rep, world):
    """The same denial one table over. `attempts_used` is how often a teammate has
    practised, which is the surveillance signal in its purest form."""
    async with as_principal(rep(world.r1b, world.m1, world.org_a)) as session:
        assert await _count(session, "assignment_recipient", assignment_id=world.assignment) == 0


async def test_transferred_rep_loses_the_old_teams_assignment(as_principal, rep, world):
    """R1a after moving to M2's team: same account, new team, old recipient row.

    Nothing clears `assignment_recipient` on a transfer, so the row that grants
    this read outlives the membership that justified it. The scope tuple is what
    changes — a post-transfer session carries the new `team_id` — so the transfer
    is modelled here by varying the scope rather than by mutating the world, which
    is session-scoped and shared with every other test in this suite.

    The same assertion covers the mirror abuse: a rep who forges a `team_id` they
    do not belong to gains nothing either, because both predicates must hold.

    Both tables, because they must agree. The drill itself is already invisible
    after a transfer — P4 matches on `team_id` — so an assignment or an allowance
    that survived would describe something the rep cannot open: a due date and a
    used/allowed count for a drill that is not there.
    """
    async with as_principal(rep(world.r1a, world.m2, world.org_a)) as session:
        assert await _count(session, "drill", id=world.published_drill) == 0
        assert await _count(session, "assignment", id=world.assignment) == 0
        assert await _count(session, "assignment_recipient", assignment_id=world.assignment) == 0


async def test_manager_still_reads_the_teams_assignment(as_principal, manager, world):
    """The rep grant must not have displaced the manager's. Policies are
    permissive and OR together, so adding one can only widen — but "can only
    widen" is a claim about the generator, and this is the table where it matters."""
    async with as_principal(manager(world.m1, world.org_a)) as session:
        assert await _count(session, "assignment", id=world.assignment) == 1


async def test_recipient_cannot_write_their_own_assignment(as_principal, rep, world):
    """SELECT, and nothing else.

    A rep who could UPDATE this row could grant themselves a later due date and a
    fresh allowance — the two things FR-TRM-012 reserves to the manager, and the
    lock FR-TRP-013 depends on. RLS makes the write match zero rows rather than
    raise, so the assertion is on the row count, not on an exception.
    """
    async with as_principal(rep(world.r1a, world.m1, world.org_a)) as session:
        updated = await session.execute(
            text(
                "update assignment set due_date = current_date + 365"
                " where id = :id returning 1"
            ),
            {"id": world.assignment},
        )
        assert updated.all() == []

        used = await session.execute(
            text(
                "update assignment_recipient set attempts_used = 0"
                " where assignment_id = :id returning 1"
            ),
            {"id": world.assignment},
        )
        assert used.all() == []


@pytest.mark.verifies("FR-TRP-010")
async def test_self_authored_attempts_are_outside_the_counted_pool(as_principal, manager, world):
    """V-1 excludes self-authored practice from ratings (FR-TRP-010).

    Asserted here rather than only at L1 because the exclusion has to survive the
    view's own RLS: a policy that leaked self-authored attempts into the counted
    pool would move a rep's rating without anyone editing an arithmetic function.
    """
    async with as_principal(manager(world.m1, world.org_a)) as session:
        rows = (
            await session.execute(
                text("select attempt_id from v_counted_attempt where drill_id = :d"),
                {"d": world.self_authored_drill},
            )
        ).all()
    assert rows == []


# ── FR-SCR-017 · concealment is lifted by authorship only ────────────────────

@pytest.mark.verifies("FR-SCR-017")
async def test_rep_cannot_read_concealed_set_of_a_published_team_drill(as_principal, rep, world):
    """The distinction between `drill` and `drill_concealed`, made concrete.

    R1a can read the published team drill — it is their team's, and they are
    meant to take it. They must not read its challenges and hidden motives, which
    are the things the drill exists to make them discover. Publication grants the
    first and not the second.
    """
    async with as_principal(rep(world.r1a, world.m1, world.org_a)) as session:
        assert await _count(session, "drill", id=world.published_drill) == 1, (
            "the rep should be able to see the team's published drill"
        )
        assert await _count(session, "drill_concealed", drill_id=world.published_drill) == 0, (
            "the rep read the CONCEALED SET of a published drill — publication does "
            "not lift concealment; only authorship does (FR-SCR-017)"
        )


@pytest.mark.verifies("FR-SCR-017")
async def test_author_reads_their_own_concealed_set(as_principal, manager, world):
    """M1 authored the published drill, so its basis is not secret from them."""
    async with as_principal(manager(world.m1, world.org_a)) as session:
        assert await _count(session, "drill_concealed", drill_id=world.published_drill) == 1


@pytest.mark.verifies("FR-SCR-017")
async def test_manager_cannot_read_a_reps_concealed_set(as_principal, manager, world):
    """Authorship lifts concealment for the *author* — not for their manager."""
    async with as_principal(manager(world.m1, world.org_a)) as session:
        assert await _count(session, "drill_concealed", drill_id=world.self_authored_drill) == 0


# ── FR-SCR-018 / AC-CND-003 · candidates see no evaluation ───────────────────

@pytest.mark.verifies("FR-SCR-018", "AC-CND-003")
@pytest.mark.parametrize(
    "table", ["scorecard", "dimension_score", "moment", "transcript_entry", "rubric_dimension"]
)
async def test_candidate_reaches_no_evaluation_table(as_principal, candidate, table):
    """Five tables, no candidate policy on any of them.

    `rubric_dimension` is in the list deliberately: no candidate surface renders
    rubric content, so its denial is structural too — the candidate's grant covers
    `drill` ROWS only (ADR-0031 §4).
    """
    async with as_principal(candidate()) as session:
        count = (await session.execute(text(f"select count(*) from {table}"))).scalar_one()
    assert count == 0, (
        f"a candidate read rows from {table} — candidates see no evaluation, ever, "
        "and that denial is an absent policy rather than an API filter"
    )


@pytest.mark.verifies("AC-CND-003")
async def test_candidate_reads_their_own_row_and_position(as_principal, candidate, world):
    """The positive half — otherwise the assessment would be unusable."""
    async with as_principal(candidate()) as session:
        assert await _count(session, "candidate", id=world.candidate) == 1
        assert await _count(session, "position", id=world.position) == 1


@pytest.mark.verifies("FR-CND-007")
async def test_candidate_cannot_read_another_candidate(as_principal, candidate, world, migration_engine):
    """One applicant must never learn who else applied."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from bluelab.platform.ids import new_id

    other = new_id()
    maker = async_sessionmaker(migration_engine, expire_on_commit=False)
    async with maker() as session, session.begin():
        await session.execute(
            text(
                "insert into candidate (id, org_id, team_id, position_id, name, email)"
                " values (:id, :org, :team, :pos, 'Other', 'other@a.test')"
            ),
            {"id": other, "org": world.org_a, "team": world.m1, "pos": world.position},
        )

    async with as_principal(candidate()) as session:
        assert await _count(session, "candidate", id=other) == 0


# ── ops holds no customer scope ──────────────────────────────────────────────

@pytest.mark.verifies("SEC-040")
@pytest.mark.parametrize(
    "table", ["transcript_entry", "scorecard", "moment", "product_fact", "drill_concealed"]
)
async def test_ops_cannot_read_customer_content(as_principal, table):
    """ADR-0010 §2 made structural.

    Ops holds the widest privilege in the system and reaches none of this. Staff
    diagnose a grading failure from ids and error classes, never from the
    transcript that failed to grade — which is what lets a small team operate the
    product without staff reading customer content.
    """
    from bluelab.platform.db.privileged import ops_scope

    async with as_principal(ops_scope()) as session:
        count = (await session.execute(text(f"select count(*) from {table}"))).scalar_one()
    assert count == 0, f"an ops principal read {table} — ADR-0010 §2 says it cannot"
