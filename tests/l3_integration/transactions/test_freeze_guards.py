"""The freeze-guard attack battery (quality/01 §4, quality/03 §5).

Every trigger gets a test that **tries to breach it and asserts refusal**. That
phrasing is the requirement, and it matters: a guard nobody attacks is a guard
nobody knows works. Coverage cannot help here — the triggers are PL/pgSQL a
Python mutation tool cannot reach — so this battery plus the generate-and-diff
drift check is the SQL equivalent of a surviving-mutant check.

Each guard is tested from three sides, because any one alone is worthless:

    the breach      the forbidden operation is REFUSED
    the permitted   the legal operation still WORKS  — otherwise a trigger that
                    rejected everything would pass every denial test
    the exemption   erasure, or a scope cascade, still crosses

Runs as the migration role, so a refusal can only have come from the trigger —
see the conftest.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

pytestmark = [
    pytest.mark.l3_integration,
    pytest.mark.l7_security,
    pytest.mark.invariant_path,
]


async def _expect_refusal(session, sql: str, params: dict, *, matching: str):
    """Run a statement that must be refused, and assert on the reason.

    Asserting the message matters: a test that accepts *any* exception passes on
    a typo, a missing column, or a constraint violation that has nothing to do
    with the guard being tested.
    """
    with pytest.raises(DBAPIError) as caught:
        async with session.begin():
            await session.execute(text(sql), params)
    message = str(caught.value)
    assert matching in message, f"refused, but not by the expected guard:\n{message}"
    await session.rollback()


# ── trg_drill_freeze ─────────────────────────────────────────────────────────

@pytest.mark.verifies("FR-DRL-015")
async def test_published_drill_content_cannot_be_edited(session, make_drill):
    drill = await make_drill(status="published")
    await _expect_refusal(
        session,
        "update drill set scenario = '{\"v\": 1, \"tampered\": true}' where id = :id",
        {"id": drill},
        matching="content is frozen",
    )


@pytest.mark.verifies("FR-DRL-015")
async def test_published_drill_label_cannot_be_edited(session, make_drill):
    """A second column, because the guard diffs the whole row rather than an
    allow-list — this proves that, instead of trusting the comment saying so."""
    drill = await make_drill(status="published")
    await _expect_refusal(
        session,
        "update drill set label = 'Renamed' where id = :id",
        {"id": drill},
        matching="content is frozen",
    )


@pytest.mark.verifies("FR-DRL-016")
async def test_published_drill_can_still_be_archived(session, make_drill):
    """The permitted move. Archive is a status change, not a content edit."""
    drill = await make_drill(status="published")
    async with session.begin():
        await session.execute(
            text("update drill set status = 'archived', archived_at = now() where id = :id"),
            {"id": drill},
        )
    status = (
        await session.execute(text("select status from drill where id = :id"), {"id": drill})
    ).scalar_one()
    assert status == "archived"


@pytest.mark.verifies("FR-DRL-015")
async def test_archived_drill_cannot_be_republished(session, make_drill):
    drill = await make_drill(status="archived")
    await _expect_refusal(
        session,
        "update drill set status = 'published' where id = :id",
        {"id": drill},
        matching="only published -> archived",
    )


@pytest.mark.verifies("FR-DRL-015")
async def test_draft_drill_is_freely_editable(session, make_drill):
    """The guard must not fire before publication — authoring depends on it."""
    drill = await make_drill(status="draft")
    async with session.begin():
        await session.execute(
            text("update drill set label = 'Draft edit' where id = :id"), {"id": drill}
        )


# ── trg_concealed_freeze ─────────────────────────────────────────────────────

@pytest.mark.verifies("FR-DRL-015", "FR-SCR-017")
async def test_concealed_set_frozen_once_published(session, make_drill):
    drill = await make_drill(status="published")
    await _expect_refusal(
        session,
        "update drill_concealed set hidden_motives = '[\"leaked\"]' where drill_id = :id",
        {"id": drill},
        matching="concealed set is frozen",
    )


@pytest.mark.verifies("FR-DRL-015")
async def test_concealed_set_cannot_be_deleted_once_published(session, make_drill):
    drill = await make_drill(status="published")
    await _expect_refusal(
        session,
        "delete from drill_concealed where drill_id = :id",
        {"id": drill},
        matching="concealed set is frozen",
    )


# ── trg_rubric_freeze ────────────────────────────────────────────────────────

@pytest.mark.verifies("FR-DRL-015")
async def test_rubric_dimension_cannot_be_added_after_publish(session, make_drill, base_org):
    """INSERT is guarded, not only UPDATE.

    Adding a dimension to a published drill would score every future attempt
    against a rubric the past was not graded on — a silent break in
    comparability that no UPDATE guard would catch.
    """
    from bluelab.platform.ids import new_id

    drill = await make_drill(status="published")
    await _expect_refusal(
        session,
        "insert into rubric_dimension (id, org_id, team_id, drill_id, ord, name, weight, rationale)"
        " values (:id, :org, :team, :drill, 99, 'Smuggled', 10, 'x')",
        {
            "id": new_id(), "org": base_org["org"], "team": base_org["manager"], "drill": drill,
        },
        matching="rubric is frozen",
    )


@pytest.mark.verifies("FR-DRL-010")
async def test_rubric_dimension_can_be_added_to_a_draft(session, make_drill, base_org):
    from bluelab.platform.ids import new_id

    drill = await make_drill(status="draft")
    async with session.begin():
        await session.execute(
            text(
                "insert into rubric_dimension (id, org_id, team_id, drill_id, ord, name, weight, rationale)"
                " values (:id, :org, :team, :drill, 1, 'Discovery', 100, 'why')"
            ),
            {"id": new_id(), "org": base_org["org"], "team": base_org["manager"], "drill": drill},
        )


# ── trg_stage_freeze ─────────────────────────────────────────────────────────

@pytest.mark.verifies("FR-HIR-005")
async def test_stage_cannot_be_added_once_assessment_frozen(session, make_position, make_drill, base_org):
    """Comparability: once one candidate is invited, the assessment is what every
    candidate on this position is judged on."""
    from bluelab.platform.ids import new_id

    position = await make_position(frozen=True)
    drill = await make_drill(status="published")
    await _expect_refusal(
        session,
        "insert into assessment_stage (id, org_id, team_id, position_id, ord, drill_id)"
        " values (:id, :org, :team, :pos, 1, :drill)",
        {
            "id": new_id(), "org": base_org["org"], "team": base_org["manager"],
            "pos": position, "drill": drill,
        },
        matching="assessment is frozen",
    )


@pytest.mark.verifies("FR-HIR-004")
async def test_stage_can_be_composed_before_the_first_invite(session, make_position, make_drill, base_org):
    from bluelab.platform.ids import new_id

    position = await make_position(frozen=False)
    drill = await make_drill(status="published")
    async with session.begin():
        await session.execute(
            text(
                "insert into assessment_stage (id, org_id, team_id, position_id, ord, drill_id)"
                " values (:id, :org, :team, :pos, 1, :drill)"
            ),
            {
                "id": new_id(), "org": base_org["org"], "team": base_org["manager"],
                "pos": position, "drill": drill,
            },
        )


# ── trg_scorecard_freeze ─────────────────────────────────────────────────────

@pytest.fixture
async def graded_attempt(session, make_drill, base_org):
    """An attempt with a scorecard and one transcript row, all committed."""
    from bluelab.platform.ids import new_id

    drill = await make_drill(status="published")
    ids = {"attempt": new_id(), "scorecard": new_id(), "rep": new_id()}
    async with session.begin():
        await session.execute(
            text(
                "insert into account (id, org_id, team_id, email, display_name, role, password_hash)"
                " values (:id, :org, :team, :email, 'Rep', 'rep', 'x')"
            ),
            {
                "id": ids["rep"], "org": base_org["org"], "team": base_org["manager"],
                "email": f"rep-{ids['rep']}@t.test",
            },
        )
        await session.execute(
            text(
                "insert into attempt (id, org_id, team_id, drill_id, rep_account_id, self_authored, status)"
                " values (:id, :org, :team, :drill, :rep, false, 'in_progress')"
            ),
            {
                "id": ids["attempt"], "org": base_org["org"], "team": base_org["manager"],
                "drill": drill, "rep": ids["rep"],
            },
        )
        # Transcript BEFORE the status flip — the ordering the guard enforces.
        await session.execute(
            text(
                "insert into transcript_entry (attempt_id, seq, org_id, team_id, speaker, at_ms, text)"
                " values (:a, 1, :org, :team, 'buyer', 0, 'مرحبا')"
            ),
            {"a": ids["attempt"], "org": base_org["org"], "team": base_org["manager"]},
        )
        await session.execute(
            text("update attempt set status = 'graded' where id = :id"), {"id": ids["attempt"]}
        )
        await session.execute(
            text(
                "insert into scorecard (id, org_id, team_id, attempt_id, overall_score)"
                " values (:id, :org, :team, :a, 7.5)"
            ),
            {
                "id": ids["scorecard"], "org": base_org["org"], "team": base_org["manager"],
                "a": ids["attempt"],
            },
        )
    return ids


@pytest.mark.verifies("FR-SCR-003", "AC-SCR-002")
async def test_scorecard_cannot_be_edited(session, graded_attempt):
    await _expect_refusal(
        session,
        "update scorecard set overall_score = 9.9 where id = :id",
        {"id": graded_attempt["scorecard"]},
        matching="insert-only",
    )


@pytest.mark.verifies("FR-SCR-003")
async def test_scorecard_cannot_be_deleted(session, graded_attempt):
    await _expect_refusal(
        session,
        "delete from scorecard where id = :id",
        {"id": graded_attempt["scorecard"]},
        matching="insert-only",
    )


@pytest.mark.verifies("FR-LIV-010", "AC-SCR-002")
async def test_transcript_cannot_be_inserted_after_the_status_flip(session, graded_attempt, base_org):
    """The ordering rule that makes T-2 atomic.

    Refusing this is what forces the application to insert the transcript BEFORE
    flipping the status — which is why the transcript can cross the call-plane
    seam exactly once, inside one transaction (ADR-0071 rule 3). Without the
    guard, a late per-segment write would look fine.
    """
    await _expect_refusal(
        session,
        "insert into transcript_entry (attempt_id, seq, org_id, team_id, speaker, at_ms, text)"
        " values (:a, 2, :org, :team, 'buyer', 100, 'late')",
        {"a": graded_attempt["attempt"], "org": base_org["org"], "team": base_org["manager"]},
        matching="has left in_progress",
    )


@pytest.mark.verifies("FR-SCR-003")
async def test_transcript_cannot_be_edited(session, graded_attempt):
    await _expect_refusal(
        session,
        "update transcript_entry set text = 'rewritten' where attempt_id = :a and seq = 1",
        {"a": graded_attempt["attempt"]},
        matching="insert-only",
    )


# ── the erasure exemption ────────────────────────────────────────────────────

@pytest.mark.verifies("CMP-001")
async def test_erasure_context_crosses_the_freeze_guard(session, graded_attempt):
    """Erasure is the ONE sanctioned writer through a freeze guard (ADR-0033).

    Without this the guards would make the product non-compliant: a scorecard
    that can never be touched is a scorecard whose quotes can never be erased.
    The person is removed and the statistical residue stands — so the guard has
    to yield to exactly one caller and no other.
    """
    async with session.begin():
        await session.execute(text("set local app.erasure_context = 'on'"))
        await session.execute(
            text("update scorecard set takeaway = null where id = :id"),
            {"id": graded_attempt["scorecard"]},
        )

    takeaway = (
        await session.execute(
            text("select takeaway from scorecard where id = :id"),
            {"id": graded_attempt["scorecard"]},
        )
    ).scalar_one()
    assert takeaway is None


@pytest.mark.verifies("ADR-0033")
async def test_erasure_context_does_not_leak_to_the_next_transaction(session, graded_attempt):
    """`SET LOCAL` scope, asserted.

    If the erasure context survived its transaction, a pooled connection would
    hand the next request a session that can rewrite frozen records. The guard
    must be back in force here.
    """
    async with session.begin():
        await session.execute(text("set local app.erasure_context = 'on'"))

    await _expect_refusal(
        session,
        "update scorecard set overall_score = 1.0 where id = :id",
        {"id": graded_attempt["scorecard"]},
        matching="insert-only",
    )


# ── trg_decision_freeze ──────────────────────────────────────────────────────

@pytest.fixture
async def shortlisted_candidate(session, make_position, base_org):
    from bluelab.platform.ids import new_id

    position = await make_position(frozen=True)
    ids = {"candidate": new_id(), "shortlist": new_id()}
    async with session.begin():
        await session.execute(
            text(
                "insert into candidate (id, org_id, team_id, position_id, name, email, decision)"
                " values (:id, :org, :team, :pos, 'C', :email, 'approved')"
            ),
            {
                "id": ids["candidate"], "org": base_org["org"], "team": base_org["manager"],
                "pos": position, "email": f"c-{ids['candidate']}@t.test",
            },
        )
        await session.execute(
            text(
                "insert into shortlist (id, org_id, team_id, position_id, sent_by, recipients, email_body)"
                " values (:id, :org, :team, :pos, :by, '[]', 'body')"
            ),
            {
                "id": ids["shortlist"], "org": base_org["org"], "team": base_org["manager"],
                "pos": position, "by": base_org["manager"],
            },
        )
        await session.execute(
            text(
                "insert into shortlist_candidate (shortlist_id, candidate_id, org_id, team_id)"
                " values (:s, :c, :org, :team)"
            ),
            {
                "s": ids["shortlist"], "c": ids["candidate"],
                "org": base_org["org"], "team": base_org["manager"],
            },
        )
    return ids


@pytest.mark.verifies("FR-HIR-013", "AC-HIR-005")
async def test_decision_frozen_once_shortlisted(session, shortlisted_candidate):
    """Membership IS the freeze. Once the report has gone to HR, the decision
    that was sent cannot be quietly revised."""
    await _expect_refusal(
        session,
        "update candidate set decision = 'rejected' where id = :id",
        {"id": shortlisted_candidate["candidate"]},
        matching="decision is frozen",
    )


@pytest.mark.verifies("FR-HIR-013")
async def test_other_candidate_fields_stay_editable_after_shortlisting(session, shortlisted_candidate):
    """Only `decision` is frozen. A manager may still annotate — a guard that
    locked the whole row would be over-broad and nobody would notice until they
    tried to edit a note."""
    async with session.begin():
        await session.execute(
            text("update candidate set internal_note = 'still editable' where id = :id"),
            {"id": shortlisted_candidate["candidate"]},
        )
