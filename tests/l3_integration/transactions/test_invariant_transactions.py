"""T-1…T-9 — the concurrency and replay battery (quality/01 §L3).

The bar: *every T-n has a concurrency test (two racing transactions → exactly one
wins, the loser gets the specified 409) and a replay test (retry → no duplicate).*

**Real concurrency, not simulated.** Each race opens two genuine sessions on
separate connections and drives them with `asyncio.gather`. Calling the function
twice in sequence would pass against code with no guard at all — the whole claim
is about what two connections do at once, so anything less is theatre.

All of it runs at `READ COMMITTED`. Correctness comes from row locks and
conditional writes, never from elevated isolation (data/02 §1) — so these tests
are also the evidence that the default isolation level is sufficient.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from bluelab.calls.admission import AdmissionRequest, ParticipantKind, admit
from bluelab.calls.completion import TranscriptRow, complete
from bluelab.calls.interruption import Disposition, interrupt
from bluelab.modules.drills.freeze import publish_drill
from bluelab.modules.hiring.invites import send_invites
from bluelab.modules.hiring.shortlist import close_position, decide, send_shortlist
from bluelab.modules.knowledge.publish import publish_facts
from bluelab.modules.review.grading import DimensionResult, write_scorecard
from bluelab.platform.errors.denial import ProblemError
from bluelab.platform.ids import new_id
from bluelab.platform.security.tokens import hash_token

pytestmark = [pytest.mark.l3_integration, pytest.mark.invariant_path]

CONSENT_VERSION = "v1"


async def _race(engine, first, second):
    """Run two operations concurrently on separate connections.

    Returns `(result_or_exception, result_or_exception)`. Exceptions are returned
    rather than raised so the test can assert which side lost and why.
    """
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def run(op):
        async with maker() as session:
            try:
                async with session.begin():
                    return await op(session)
            except Exception as exc:  # noqa: BLE001
                # Deliberately blind. This helper races two transactions and hands
                # BOTH outcomes back so the test can assert which one lost and how.
                # Narrowing it would mean predicting the failure — and the point of
                # a race test is that the loser's exception is the finding.
                return exc

    return await asyncio.gather(run(first), run(second))


def _winners(results) -> tuple[list, list]:
    losers = [r for r in results if isinstance(r, Exception)]
    winners = [r for r in results if not isinstance(r, Exception)]
    return winners, losers


# ── T-1 · admission ──────────────────────────────────────────────────────────

@pytest.mark.verifies("FR-TRP-013", "AC-TRP-005")
async def test_t1_allowance_cas_admits_exactly_one_of_two_racing_tabs(
    engine, session, base_org, make_drill
):
    """The last remaining attempt goes to exactly one of two simultaneous tabs.

    This is the test the CAS exists for. A read-then-write implementation passes
    every sequential test and fails this one: both tabs read `attempts_used = 0`,
    both see room, both insert.
    """
    drill = await make_drill(status="published")
    rep, assignment = new_id(), new_id()

    async with session.begin():
        await session.execute(
            text(
                "insert into account (id, org_id, team_id, email, display_name, role, password_hash)"
                " values (:id, :org, :team, :email, 'R', 'rep', 'x')"
            ),
            {"id": rep, "org": base_org["org"], "team": base_org["manager"],
             "email": f"race-{rep}@t.test"},
        )
        await session.execute(
            text(
                "insert into consent_record (id, org_id, account_id, notice_version)"
                " values (:id, :org, :who, :v)"
            ),
            {"id": new_id(), "org": base_org["org"], "who": rep, "v": CONSENT_VERSION},
        )
        await session.execute(
            text(
                "insert into assignment (id, org_id, team_id, drill_id, due_date,"
                " attempts_allowed, created_by)"
                " values (:id, :org, :team, :drill, current_date, 1, :by)"
            ),
            {"id": assignment, "org": base_org["org"], "team": base_org["manager"],
             "drill": drill, "by": base_org["manager"]},
        )
        await session.execute(
            text(
                "insert into assignment_recipient (assignment_id, org_id, team_id, rep_account_id)"
                " values (:a, :org, :team, :rep)"
            ),
            {"a": assignment, "org": base_org["org"], "team": base_org["manager"], "rep": rep},
        )

    request = AdmissionRequest(
        kind=ParticipantKind.REP,
        org_id=base_org["org"],
        team_id=base_org["manager"],
        drill_id=drill,
        account_id=rep,
    )
    op = lambda s: admit(s, request, consent_version=CONSENT_VERSION)

    winners, losers = _winners(await _race(engine, op, op))

    assert len(winners) == 1, "both tabs consumed the same single attempt"
    assert len(losers) == 1
    assert isinstance(losers[0], ProblemError)
    assert losers[0].problem.slug == "allowance-exhausted"


@pytest.mark.verifies("FR-LIV-004", "CMP-002")
async def test_t1_refuses_without_consent_on_record(session, base_org, make_drill):
    """The universal backstop (AC-LIV-007). Recording without consent is a
    CMP-002 breach, so admission checks even though a gate upstream should have."""
    drill = await make_drill(status="published")
    rep = new_id()
    async with session.begin():
        await session.execute(
            text(
                "insert into account (id, org_id, team_id, email, display_name, role, password_hash)"
                " values (:id, :org, :team, :email, 'R', 'rep', 'x')"
            ),
            {"id": rep, "org": base_org["org"], "team": base_org["manager"],
             "email": f"noconsent-{rep}@t.test"},
        )

    with pytest.raises(ProblemError) as caught:
        async with session.begin():
            await admit(
                session,
                AdmissionRequest(
                    kind=ParticipantKind.AUTHOR,
                    org_id=base_org["org"],
                    team_id=base_org["manager"],
                    drill_id=drill,
                    account_id=rep,
                ),
                consent_version=CONSENT_VERSION,
            )
    assert caught.value.problem.slug == "consent-required"
    await session.rollback()


# ── T-2 · completion ─────────────────────────────────────────────────────────

@pytest.fixture
async def in_progress_attempt(session, base_org, make_drill):
    drill = await make_drill(status="published")
    ids = {"attempt": new_id(), "rep": new_id(), "drill": drill}
    async with session.begin():
        await session.execute(
            text(
                "insert into account (id, org_id, team_id, email, display_name, role, password_hash)"
                " values (:id, :org, :team, :email, 'R', 'rep', 'x')"
            ),
            {"id": ids["rep"], "org": base_org["org"], "team": base_org["manager"],
             "email": f"live-{ids['rep']}@t.test"},
        )
        await session.execute(
            text(
                "insert into attempt (id, org_id, team_id, drill_id, rep_account_id,"
                " self_authored, status)"
                " values (:id, :org, :team, :drill, :rep, false, 'in_progress')"
            ),
            {"id": ids["attempt"], "org": base_org["org"], "team": base_org["manager"],
             "drill": drill, "rep": ids["rep"]},
        )
    return ids


_ENDED_AT = text(
    "select started_at + make_interval(secs => :seconds) from attempt where id = :attempt"
)


async def _ended_at(session, attempt_id, seconds: int) -> datetime:
    """A call's end time, derived from its own start on the DATABASE's clock.

    The same discipline `test_t7_expiry_follows_the_positions_own_window` already
    applies below — it compares against `now()` in SQL precisely because "the
    host/WSL clock skew makes tighter timestamp assertions flaky here". T-2 was
    the one place that still reached for the host clock.

    `attempt.started_at` defaults to postgres `now()`, and `ck_attempt_ends_after_start`
    compares the two. Passing `datetime.now(UTC)` here compared the HOST clock
    against the DATABASE clock, so the constraint held only while two machines
    agreed to within the few milliseconds these tests take — and on a WSL setup
    they do not. It failed roughly two runs in three at a 166ms offset, in the
    direction where postgres runs ahead and the attempt appears to end before it
    started.

    Deriving from `started_at` removes the second clock entirely rather than
    tolerating it, and makes the row coherent besides: a call of
    `duration_seconds` now ends exactly that long after it began.
    """
    return (
        await session.execute(_ENDED_AT, {"seconds": seconds, "attempt": attempt_id})
    ).scalar_one()


@pytest.mark.verifies("FR-LIV-013", "FR-SCR-009")
async def test_t2_replay_writes_nothing_twice(engine, session, base_org, in_progress_attempt):
    """A lost completion request is a real failure mode (ADR-0071 rule 7).

    The runtime, the lease sweep and the LiveKit webhook are three paths to the
    same completion. The second must be a no-op — not a duplicate transcript and
    not a second grading job.
    """
    rows = [TranscriptRow(seq=1, speaker="buyer", at_ms=0, text="مرحبا")]

    async def op(s):
        return await complete(
            s,
            attempt_id=in_progress_attempt["attempt"],
            org_id=base_org["org"],
            team_id=base_org["manager"],
            rows=rows,
            ended_at=await _ended_at(s, in_progress_attempt["attempt"], 300),
            duration_seconds=300,
            recording_object_key="rec/1",
        )

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s, s.begin():
        assert await op(s) is True
    async with maker() as s, s.begin():
        assert await op(s) is False, "a replayed completion was treated as a new one"

    transcripts = (
        await session.execute(
            text("select count(*) from transcript_entry where attempt_id = :a"),
            {"a": in_progress_attempt["attempt"]},
        )
    ).scalar_one()
    jobs = (
        await session.execute(
            text(
                "select count(*) from procrastinate_jobs"
                " where args->'args'->>'attempt_id' = :a"
            ),
            {"a": str(in_progress_attempt["attempt"])},
        )
    ).scalar_one()

    assert transcripts == 1, "the replay duplicated the transcript"
    assert jobs == 1, "the replay enqueued a second grading job"


@pytest.mark.verifies("FR-LIV-010", "ADR-0023")
async def test_t2_transcript_and_job_commit_together(session, base_org, in_progress_attempt):
    """Transactional enqueue, asserted by rolling back (ADR-0023).

    If the job row survived a rolled-back domain write, the outbox problem would
    be back and a grading job would reference an attempt that never completed.
    """
    async with session.begin():
        await complete(
            session,
            attempt_id=in_progress_attempt["attempt"],
            org_id=base_org["org"],
            team_id=base_org["manager"],
            rows=[TranscriptRow(seq=1, speaker="buyer", at_ms=0, text="x")],
            ended_at=await _ended_at(session, in_progress_attempt["attempt"], 10),
            duration_seconds=10,
            recording_object_key=None,
        )
        await session.rollback()

    for relation, column in (("transcript_entry", "attempt_id"), ):
        count = (
            await session.execute(
                text(f"select count(*) from {relation} where {column} = :a"),
                {"a": in_progress_attempt["attempt"]},
            )
        ).scalar_one()
        assert count == 0
    jobs = (
        await session.execute(
            text("select count(*) from procrastinate_jobs where args->'args'->>'attempt_id' = :a"),
            {"a": str(in_progress_attempt["attempt"])},
        )
    ).scalar_one()
    assert jobs == 0, "the job row outlived the rolled-back domain write"


# ── T-3 · grading ────────────────────────────────────────────────────────────

@pytest.mark.verifies("FR-SCR-003")
async def test_t3_two_graders_produce_one_scorecard(engine, session, base_org, make_drill):
    """Grade exactly once. The queue is at-least-once, so this WILL happen."""
    # Built through the real lifecycle: draft -> rubric -> publish -> attempt.
    # Two earlier shortcuts were both refused by the freeze guards — adding a
    # rubric to a published drill, then un-publishing one to get around that.
    # Neither is a legal transition, and the guards said so. The lifecycle is the
    # only way in.
    drill = await make_drill(status="draft")
    dimension, rep, attempt = new_id(), new_id(), new_id()

    async with session.begin():
        await session.execute(
            text(
                "insert into rubric_dimension (id, org_id, team_id, drill_id, ord, name,"
                " weight, rationale) values (:id, :org, :team, :drill, 1, 'D', 100, 'r')"
            ),
            {"id": dimension, "org": base_org["org"], "team": base_org["manager"], "drill": drill},
        )
        await session.execute(
            text(
                "update drill set status = 'published', published_at = now(),"
                " label = 'Buyer', scenario = '{\"v\": 1}', answer_key = '{\"v\": 1}'"
                " where id = :d"
            ),
            {"d": drill},
        )
        await session.execute(
            text(
                "insert into account (id, org_id, team_id, email, display_name, role, password_hash)"
                " values (:id, :org, :team, :email, 'R', 'rep', 'x')"
            ),
            {"id": rep, "org": base_org["org"], "team": base_org["manager"],
             "email": f"grade-{rep}@t.test"},
        )
        await session.execute(
            text(
                "insert into attempt (id, org_id, team_id, drill_id, rep_account_id,"
                " self_authored, status)"
                " values (:id, :org, :team, :drill, :rep, false, 'completed')"
            ),
            {"id": attempt, "org": base_org["org"], "team": base_org["manager"],
             "drill": drill, "rep": rep},
        )

    from decimal import Decimal

    async def op(s):
        return await write_scorecard(
            s,
            attempt_id=attempt,
            org_id=base_org["org"],
            team_id=base_org["manager"],
            dimensions=[DimensionResult(dimension, 100, Decimal("7.5"), "note")],
            moments=[],
            takeaway="t",
            grading_meta={"model": "test"},
        )

    results = await _race(engine, op, op)
    assert not any(isinstance(r, Exception) for r in results), (
        f"a losing grader raised instead of losing silently: {results}"
    )
    assert sorted(r is None for r in results) == [False, True], (
        "exactly one grader should win and the other return None"
    )

    scorecards = (
        await session.execute(
            text("select count(*) from scorecard where attempt_id = :a"), {"a": attempt}
        )
    ).scalar_one()
    assert scorecards == 1


# ── T-4 · knowledge publish ──────────────────────────────────────────────────

@pytest.fixture
async def document_with_draft(session, base_org):
    ids = {"doc": new_id(), "live": new_id(), "draft": new_id()}
    async with session.begin():
        await session.execute(
            text(
                "insert into product_document (id, org_id, team_id, title, live_version)"
                " values (:id, :org, :team, 'Tiers', 0)"
            ),
            {"id": ids["doc"], "org": base_org["org"], "team": base_org["manager"]},
        )
        for key, kind, base in (("live", "live", None), ("draft", "draft", 0)):
            await session.execute(
                text(
                    "insert into fact_set (id, org_id, team_id, document_id, kind, source,"
                    " based_on_version, created_by)"
                    " values (:id, :org, :team, :doc, :kind, 'manual', :base, :by)"
                ),
                {"id": ids[key], "org": base_org["org"], "team": base_org["manager"],
                 "doc": ids["doc"], "kind": kind, "base": base, "by": base_org["manager"]},
            )
    return ids


@pytest.mark.verifies("FR-KNW-011", "AC-KNW-005")
async def test_t4_second_publisher_gets_stale_review(engine, document_with_draft):
    """Two managers confirming the same diff: one publishes, one is told to look
    again. Last-write-wins would publish facts the second never reviewed."""
    async def op(s):
        return await publish_facts(s, document_id=document_with_draft["doc"], based_on_version=0)

    winners, losers = _winners(await _race(engine, op, op))
    assert len(winners) == 1
    assert isinstance(losers[0], ProblemError)
    assert losers[0].problem.slug in ("stale-review", "no-draft-to-review")


@pytest.mark.verifies("FR-KNW-006")
async def test_t4_leaves_exactly_one_live_set(session, document_with_draft):
    async with session.begin():
        await publish_facts(session, document_id=document_with_draft["doc"], based_on_version=0)

    live = (
        await session.execute(
            text("select count(*) from fact_set where document_id = :d and kind = 'live'"),
            {"d": document_with_draft["doc"]},
        )
    ).scalar_one()
    assert live == 1


# ── T-5 · drill publish ──────────────────────────────────────────────────────

@pytest.mark.verifies("FR-DRL-010", "AC-DRL-003")
async def test_t5_refuses_weights_that_do_not_total_100(session, base_org, make_drill):
    drill = await make_drill(status="draft")
    async with session.begin():
        await session.execute(
            text(
                "insert into rubric_dimension (id, org_id, team_id, drill_id, ord, name,"
                " weight, rationale) values (:id, :org, :team, :drill, 1, 'D', 97, 'r')"
            ),
            {"id": new_id(), "org": base_org["org"], "team": base_org["manager"], "drill": drill},
        )

    with pytest.raises(ProblemError) as caught:
        async with session.begin():
            await publish_drill(
                session, drill_id=drill, team_id=base_org["manager"],
                scenario={"persona": "x"}, label="Buyer", content_hash="h",
            )
    assert caught.value.problem.slug == "weights-not-100"
    assert caught.value.meta["delta"] == 3, "the UI renders '+3 to balance' from this"
    await session.rollback()


@pytest.mark.verifies("FR-DRL-015")
async def test_t5_second_publish_is_refused(engine, session, base_org, make_drill):
    drill = await make_drill(status="draft")
    async with session.begin():
        await session.execute(
            text(
                "insert into rubric_dimension (id, org_id, team_id, drill_id, ord, name,"
                " weight, rationale) values (:id, :org, :team, :drill, 1, 'D', 100, 'r')"
            ),
            {"id": new_id(), "org": base_org["org"], "team": base_org["manager"], "drill": drill},
        )

    async def op(s):
        return await publish_drill(
            s, drill_id=drill, team_id=base_org["manager"],
            scenario={"persona": "x"}, label="Buyer", content_hash="h",
        )

    winners, losers = _winners(await _race(engine, op, op))
    assert len(winners) == 1, "a drill was published twice — content would be rewritten"
    assert isinstance(losers[0], ProblemError)


# ── T-6 · interruption ───────────────────────────────────────────────────────

@pytest.mark.verifies("FR-LIV-015")
async def test_t6_double_interruption_reverses_the_allowance_once(
    engine, session, base_org, make_drill
):
    """Void and free — once. The sweep and the runtime can both report the same
    interruption, and a double reversal hands out an attempt nobody earned."""
    drill = await make_drill(status="published")
    rep, assignment, attempt = new_id(), new_id(), new_id()
    async with session.begin():
        await session.execute(
            text(
                "insert into account (id, org_id, team_id, email, display_name, role, password_hash)"
                " values (:id, :org, :team, :email, 'R', 'rep', 'x')"
            ),
            {"id": rep, "org": base_org["org"], "team": base_org["manager"],
             "email": f"int-{rep}@t.test"},
        )
        await session.execute(
            text(
                "insert into assignment (id, org_id, team_id, drill_id, due_date,"
                " attempts_allowed, created_by)"
                " values (:id, :org, :team, :drill, current_date, 3, :by)"
            ),
            {"id": assignment, "org": base_org["org"], "team": base_org["manager"],
             "drill": drill, "by": base_org["manager"]},
        )
        await session.execute(
            text(
                "insert into assignment_recipient (assignment_id, org_id, team_id,"
                " rep_account_id, attempts_used) values (:a, :org, :team, :rep, 1)"
            ),
            {"a": assignment, "org": base_org["org"], "team": base_org["manager"], "rep": rep},
        )
        await session.execute(
            text(
                "insert into attempt (id, org_id, team_id, drill_id, rep_account_id,"
                " self_authored, status)"
                " values (:id, :org, :team, :drill, :rep, false, 'in_progress')"
            ),
            {"id": attempt, "org": base_org["org"], "team": base_org["manager"],
             "drill": drill, "rep": rep},
        )

    maker = async_sessionmaker(engine, expire_on_commit=False)
    for expected in (True, False):
        async with maker() as s, s.begin():
            got = await interrupt(
                s, attempt_id=attempt, drill_id=drill, rep_account_id=rep,
                disposition=Disposition.PARTICIPANT_DROPPED,
            )
        assert got is expected

    used = (
        await session.execute(
            text("select attempts_used from assignment_recipient where rep_account_id = :r"),
            {"r": rep},
        )
    ).scalar_one()
    assert used == 0, "the allowance was reversed twice"


# ── T-7 · first invite send ──────────────────────────────────────────────────
#
# The freeze is the invariant here, and it is a comparability claim rather than a
# storage one: `assessment_frozen_at` is the evidence of *when* the assessment
# became fixed, and FR-HIR-005 rests on every candidate having faced the same one.
# A second invite that moved the timestamp would leave two candidates provably
# assessed against the same drills and unprovably against the same assessment.


@pytest.fixture
def make_assessment(session, base_org, make_drill):
    """A position with one stage and N candidates — the minimum T-7 will accept.

    A position with no stages is a *different* fixture on purpose: `send_invites`
    refuses it, and a helper that always produced a valid one would leave the
    refusal path untestable.
    """

    async def _make(*, candidates: int = 1, stages: int = 1, status: str = "active"):
        position = new_id()
        drill = await make_drill(status="published")
        candidate_ids = [new_id() for _ in range(candidates)]
        async with session.begin():
            await session.execute(
                text(
                    "insert into position (id, org_id, team_id, title, openings, status)"
                    " values (:id, :org, :team, 'AE', 1, :status)"
                ),
                {"id": position, "org": base_org["org"], "team": base_org["manager"],
                 "status": status},
            )
            for ordinal in range(stages):
                await session.execute(
                    text(
                        "insert into assessment_stage (id, org_id, team_id, position_id, ord,"
                        " drill_id) values (:id, :org, :team, :pos, :ord, :drill)"
                    ),
                    {"id": new_id(), "org": base_org["org"], "team": base_org["manager"],
                     "pos": position, "ord": ordinal, "drill": drill},
                )
            for candidate in candidate_ids:
                await session.execute(
                    text(
                        "insert into candidate (id, org_id, team_id, position_id, name, email)"
                        " values (:id, :org, :team, :pos, 'C', :email)"
                    ),
                    {"id": candidate, "org": base_org["org"], "team": base_org["manager"],
                     "pos": position, "email": f"inv-{candidate}@t.test"},
                )
        return position, candidate_ids

    return _make


def _invite(position, org, team, candidates):
    async def _op(s):
        return await send_invites(
            s, position_id=position, org_id=org, team_id=team, candidate_ids=candidates
        )

    return _op


@pytest.mark.verifies("FR-HIR-004")
async def test_t7_refuses_an_assessment_with_no_stages(session, base_org, make_assessment):
    """Inviting into an empty assessment sends a candidate a link to nothing."""
    position, candidates = await make_assessment(stages=0)

    with pytest.raises(ProblemError) as caught:
        async with session.begin():
            await _invite(position, base_org["org"], base_org["manager"], candidates)(session)

    assert caught.value.problem.slug == "assessment-empty"


@pytest.mark.verifies("FR-HIR-015")
async def test_t7_refuses_a_closed_position(session, base_org, make_assessment):
    position, candidates = await make_assessment(status="closed")

    with pytest.raises(ProblemError) as caught:
        async with session.begin():
            await _invite(position, base_org["org"], base_org["manager"], candidates)(session)

    assert caught.value.problem.slug == "position-closed"


@pytest.mark.verifies("FR-HIR-005")
async def test_t7_first_invite_freezes_the_assessment(session, base_org, make_assessment):
    """The permitted case, asserted alongside the two refusals above — otherwise a
    function that refused everything would pass both of them."""
    position, candidates = await make_assessment()

    async with session.begin():
        issued = await _invite(position, base_org["org"], base_org["manager"], candidates)(session)

    frozen_at = (
        await session.execute(
            text("select assessment_frozen_at from position where id = :p"), {"p": position}
        )
    ).scalar_one()
    assert frozen_at is not None
    assert len(issued) == 1


@pytest.mark.verifies("FR-HIR-005")
async def test_t7_a_second_invite_does_not_move_the_freeze(session, base_org, make_assessment):
    """`coalesce(assessment_frozen_at, now())`, and the reason for it.

    Comparability rests on every candidate having faced the same assessment, so
    the timestamp must record the first freeze forever. A bare assignment would
    pass every other test in this block and quietly destroy that.
    """
    position, candidates = await make_assessment(candidates=2)
    org, team = base_org["org"], base_org["manager"]

    frozen = text("select assessment_frozen_at from position where id = :p")

    # Read inside the writing transaction: a bare read between two `begin()`
    # blocks autobegins a transaction of its own, and the next `begin()` then
    # raises rather than testing anything.
    async with session.begin():
        await _invite(position, org, team, [candidates[0]])(session)
        first = (await session.execute(frozen, {"p": position})).scalar_one()

    async with session.begin():
        await _invite(position, org, team, [candidates[1]])(session)
        second = (await session.execute(frozen, {"p": position})).scalar_one()

    assert second == first, "the second invite moved the freeze timestamp"


@pytest.mark.verifies("FR-HIR-005")
async def test_t7_two_racing_first_invites_agree_on_one_freeze(
    engine, session, base_org, make_assessment
):
    """The concurrency half of the bar.

    Both sides legitimately succeed — T-7 is not exclusive — so the claim is not
    "one wins" but "they cannot disagree". Two connections racing on the same
    position must leave a single freeze instant behind, which is what the row lock
    plus `coalesce` buys.
    """
    position, candidates = await make_assessment(candidates=2)
    org, team = base_org["org"], base_org["manager"]

    results = await _race(
        engine,
        _invite(position, org, team, [candidates[0]]),
        _invite(position, org, team, [candidates[1]]),
    )
    winners, losers = _winners(results)
    assert not losers, f"neither side should be refused: {losers}"
    assert len(winners) == 2

    instants = (
        await session.execute(
            text("select count(distinct assessment_frozen_at) from position where id = :p"),
            {"p": position},
        )
    ).scalar_one()
    assert instants == 1


@pytest.mark.verifies("SEC-023", "FR-HIR-004")
async def test_t7_persists_only_the_token_hash(session, base_org, make_assessment):
    """The plaintext is emailed once and never stored. A database read must not
    yield anything a stolen backup could turn into a working invite link."""
    position, candidates = await make_assessment()

    async with session.begin():
        issued = await _invite(position, base_org["org"], base_org["manager"], candidates)(session)

    stored = (
        await session.execute(
            text("select token_hash from candidate_token where id = :t"),
            {"t": issued[0].token_id},
        )
    ).scalar_one()
    assert stored == hash_token(issued[0].plaintext_token)
    assert stored != issued[0].plaintext_token

    leaked = (
        await session.execute(
            text("select count(*) from candidate_token where token_hash = :p"),
            {"p": issued[0].plaintext_token},
        )
    ).scalar_one()
    assert leaked == 0


@pytest.mark.verifies("FR-HIR-004")
async def test_t7_expiry_follows_the_positions_own_window(session, base_org, make_assessment):
    """The window comes from the position, not from a constant.

    One mutant here is **equivalent and cannot be killed**: `datetime.now(UTC)` →
    `datetime.now(None)`. The naive value lands in a `timestamptz` column and is
    read in the session's TimeZone, which on both this box and CI matches the
    process's own — so the two expressions store the identical instant. Do not
    contort this test chasing it. It is still worth keeping `UTC` explicit: the
    equivalence holds only while those two zones agree, and nothing enforces that.
    """
    position, candidates = await make_assessment()
    async with session.begin():
        await session.execute(
            text("update position set invite_expiry_days = 3 where id = :p"), {"p": position}
        )

    async with session.begin():
        issued = await _invite(position, base_org["org"], base_org["manager"], candidates)(session)

    # Compared against the DATABASE's clock, and to within an hour rather than to
    # the nearest day. Rounding to days would accept a naive `datetime.now()` —
    # local time written into a timestamptz column, which on this box is three
    # hours adrift. An hour is still orders of magnitude above the host/WSL clock
    # skew that makes tighter timestamp assertions flaky here.
    drift = (
        await session.execute(
            text(
                "select abs(extract(epoch from (expires_at - (now() + interval '3 days'))))"
                "  from candidate_token where id = :t"
            ),
            {"t": issued[0].token_id},
        )
    ).scalar_one()
    assert float(drift) < 3600, f"expiry is {float(drift) / 3600:.1f}h from the position's window"


@pytest.mark.verifies("FR-IDA-012")
async def test_t7_a_resend_issues_a_fresh_token_and_the_prior_stays_valid(
    session, base_org, make_assessment
):
    """A resend is a new token, not a replacement. The candidate who finally opens
    the first email must not find it dead."""
    position, candidates = await make_assessment()
    org, team = base_org["org"], base_org["manager"]

    async with session.begin():
        first = await _invite(position, org, team, candidates)(session)
    async with session.begin():
        second = await _invite(position, org, team, candidates)(session)

    assert first[0].token_id != second[0].token_id
    assert first[0].plaintext_token != second[0].plaintext_token

    live = (
        await session.execute(
            text(
                "select count(*) from candidate_token"
                " where candidate_id = :c and revoked_at is null"
            ),
            {"c": candidates[0]},
        )
    ).scalar_one()
    assert live == 2, "the resend revoked or replaced the prior token"


@pytest.mark.verifies("FR-HIR-010")
async def test_t7_every_candidate_gets_a_send_and_a_dispatch_job(
    session, base_org, make_assessment
):
    """One `email_send` and one `dispatch_email` job per recipient.

    The batch is the unit of the request, never of the effect. A single job for a
    batch of three leaves two candidates with a token, a database row saying an
    invite was queued, and no email — indistinguishable in the pipeline view from
    a candidate who ignored one (FR-HIR-010).
    """
    position, candidates = await make_assessment(candidates=3)

    async with session.begin():
        issued = await _invite(position, base_org["org"], base_org["manager"], candidates)(session)

    assert len(issued) == 3
    assert [i.candidate_id for i in issued] == candidates, (
        "each issued invite must name the candidate it was minted for"
    )

    sends = (
        await session.execute(
            text(
                "select count(*) from email_send"
                " where kind = 'E2_invite' and candidate_id = any(:c)"
            ),
            {"c": list(candidates)},
        )
    ).scalar_one()
    assert sends == 3

    # The envelope, not just the payload. `org_id` is what the worker resolves its
    # scope from — the work plane has no unscoped path (ADR-0005) — so a job
    # carrying the wrong one is a job that runs against the wrong tenant or not at
    # all. Asserting only the payload leaves that unguarded.
    queued = (
        await session.execute(
            text(
                "select args -> 'args' ->> 'email_send_id',"
                "       args ->> 'org_id', args ->> 'team_id'"
                "  from procrastinate_jobs"
                " where task_name like '%dispatch_email%'"
                "   and args -> 'args' ->> 'email_send_id' = any(:ids)"
            ),
            {"ids": [str(i.email_send_id) for i in issued]},
        )
    ).all()

    assert sorted(row[0] for row in queued) == sorted(str(i.email_send_id) for i in issued), (
        "every recipient needs its own dispatch job, carrying its own email_send id"
    )
    assert {row[1] for row in queued} == {str(base_org["org"])}
    assert {row[2] for row in queued} == {str(base_org["manager"])}


@pytest.mark.verifies("FR-HIR-010")
async def test_t7_an_empty_batch_enqueues_nothing(session, base_org, make_assessment):
    """The `if issued:` branch. Nothing to send is not the same as something to
    send, and an empty batch that still queued a job would dispatch against a row
    that does not exist."""
    position, _ = await make_assessment()

    jobs = text("select count(*) from procrastinate_jobs")

    before = (await session.execute(jobs)).scalar_one()
    await session.rollback()  # the read autobegan; release it before the next begin

    async with session.begin():
        issued = await _invite(position, base_org["org"], base_org["manager"], [])(session)
        after = (await session.execute(jobs)).scalar_one()

    assert issued == []
    assert after == before


@pytest.mark.verifies("FR-HIR-010")
async def test_t7_a_replayed_send_for_one_token_is_absorbed(session, base_org, make_assessment):
    """The `email_send (kind, dedupe_key)` backstop, reached directly.

    `send_invites` mints a fresh token per call, so it cannot drive this branch
    itself — which is exactly why the branch needs its own test. The dedupe key is
    the TOKEN, so this is what stops a retried *delivery* of one issued invite
    becoming a second email.
    """
    position, candidates = await make_assessment()
    async with session.begin():
        issued = await _invite(position, base_org["org"], base_org["manager"], candidates)(session)

    async with session.begin():
        await session.execute(
            text(
                "insert into email_send (id, org_id, kind, dedupe_key, candidate_id, token_id)"
                " values (:id, :org, 'E2_invite', :dedupe, :c, :t)"
                " on conflict (kind, dedupe_key) do nothing"
            ),
            {"id": new_id(), "org": base_org["org"], "dedupe": str(issued[0].token_id),
             "c": candidates[0], "t": issued[0].token_id},
        )

    sends = (
        await session.execute(
            text("select count(*) from email_send where dedupe_key = :d"),
            {"d": str(issued[0].token_id)},
        )
    ).scalar_one()
    assert sends == 1


# ── T-8 / T-9 · close, decide, shortlist ─────────────────────────────────────

@pytest.mark.verifies("FR-HIR-013", "AC-HIR-005")
async def test_t9_decision_frozen_after_shortlist(session, base_org, make_position):
    position = await make_position(frozen=True)
    candidate = new_id()
    async with session.begin():
        await session.execute(
            text(
                "insert into candidate (id, org_id, team_id, position_id, name, email, decision)"
                " values (:id, :org, :team, :pos, 'C', :email, 'approved')"
            ),
            {"id": candidate, "org": base_org["org"], "team": base_org["manager"],
             "pos": position, "email": f"d-{candidate}@t.test"},
        )

    async with session.begin():
        await send_shortlist(
            session, position_id=position, org_id=base_org["org"],
            team_id=base_org["manager"], sent_by=base_org["manager"],
            candidate_ids=[candidate], recipients=[{"email": "hr@t.test"}], email_body="b",
        )

    with pytest.raises(ProblemError) as caught:
        async with session.begin():
            await decide(session, candidate_id=candidate, decision="rejected")
    assert caught.value.problem.slug in ("decision-frozen", "candidate-not-decidable")
    await session.rollback()


@pytest.mark.verifies("FR-HIR-015")
async def test_t8_close_is_idempotent_and_revokes_tokens(engine, session, base_org, make_position):
    position = await make_position(frozen=True)
    candidate, token = new_id(), new_id()
    async with session.begin():
        await session.execute(
            text(
                "insert into candidate (id, org_id, team_id, position_id, name, email)"
                " values (:id, :org, :team, :pos, 'C', :email)"
            ),
            {"id": candidate, "org": base_org["org"], "team": base_org["manager"],
             "pos": position, "email": f"tok-{candidate}@t.test"},
        )
        await session.execute(
            text(
                "insert into candidate_token (id, org_id, team_id, candidate_id, token_hash,"
                " expires_at) values (:id, :org, :team, :c, :h, now() + interval '7 days')"
            ),
            {"id": token, "org": base_org["org"], "team": base_org["manager"],
             "c": candidate, "h": f"hash-{token}"},
        )
        await session.execute(
            text("update position set status = 'active' where id = :p"), {"p": position}
        )

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s, s.begin():
        assert await close_position(s, position_id=position) is True
    async with maker() as s, s.begin():
        assert await close_position(s, position_id=position) is False, (
            "a replayed close should be an idempotent no-op (api/00 §6)"
        )

    revoked = (
        await session.execute(
            text("select revoked_at is not null from candidate_token where id = :t"), {"t": token}
        )
    ).scalar_one()
    assert revoked is True
