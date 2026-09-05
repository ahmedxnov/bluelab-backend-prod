"""Fixtures for the rep surface (L3 — real postgres, real RLS, real views).

These drive the actual ASGI app against the actual database, so what passes here
is evidence about the deployed path. The V-1…V-11 views are `security_invoker`,
which means a read runs under the caller's scope — seeding through the migration
role and reading through the app role is what makes that assertion real rather
than incidental.

## The world is built around one distinction

`v_counted_attempt` excludes self-authored practice, and almost every number on
this surface is derived from it. So the world seeds BOTH kinds for the same rep:
graded team attempts that must count, and a graded self-authored attempt that
must not. A fixture with only the first would let a regression that dropped the
`not self_authored` predicate pass silently — and that predicate is AC-TRP-004,
the rep's private rehearsal staying private.

## Scores are chosen to sit inside bands, not on their edges

`fn_score_band` cuts at 7.5 and 5.5. Seeded scores avoid those exact values so a
band assertion tests the plumbing rather than a rounding tie — the banding rule
itself has its own home, and it is not this suite.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

import fakeredis.aioredis
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tests.support import required_url

from bluelab.platform.config import Settings
from bluelab.platform.ids import new_id
from bluelab.platform.security.passwords import hash_password

MIGRATION_URL = required_url("TEST_MIGRATION_URL")
APP_URL = required_url("TEST_DATABASE_URL")

os.environ.setdefault("DATABASE_URL", APP_URL)
os.environ.setdefault("VALKEY_URL", "redis://127.0.0.1:6379/0")
os.environ.setdefault("AGENT_HMAC_SECRET", "test-only-not-a-real-secret")

PASSWORD = "correct-horse-battery-staple"  # pragma: allowlist secret
ENCODED_PASSWORD = hash_password(PASSWORD)
"""Hashed once for the module — argon2id is ~50ms a call by design."""


def app_settings(**overrides: object) -> Settings:
    """`staging`, so the `__Host-` cookie's required `Secure` is set."""
    return Settings(
        BLUELAB_ENV="staging",
        DATABASE_URL=APP_URL,
        VALKEY_URL="redis://127.0.0.1:6379/0",  # never dialled — fakeredis stands in
        AGENT_HMAC_SECRET="test-only-not-a-real-secret",  # pragma: allowlist secret
        **overrides,
    )


@dataclass(frozen=True, slots=True)
class World:
    org: UUID
    manager: UUID
    manager_email: str
    """A manager with no attempts of their own — so `/me` returning anything for
    them means their team was aggregated into their personal numbers."""
    rep: UUID
    rep_email: str
    other_rep: UUID
    other_rep_email: str
    drill_discovery: UUID
    drill_renewal: UUID
    self_drill: UUID
    """A self-authored drill. Its graded attempt must never reach a rating."""
    self_draft: UUID
    """The rep's OWN unpublished draft. Appears in Created-by-me marked
    `status=draft` and in no other principal's surface (FR-DRL-013, gate F-1)."""
    manager_draft: UUID
    """The MANAGER's draft. Belongs to `GET /team/drills`, and must appear on
    nobody's `/me/library` — including the manager's own."""
    archived_drill: UUID
    """Archived is withdrawal (FR-DRL-016). Invisible to everyone here."""
    assignment: UUID
    """On `drill_renewal`, granted to `rep` with 1 of 3 used — so the card is
    assigned and NOT locked."""
    locked_assignment: UUID
    """On `drill_discovery`, granted to `rep` with 3 of 3 used — the exhausted
    case FR-TRP-013 renders locked. `other_rep` holds the same assignment with 0
    used, so a query missing its per-account predicate reads the wrong counter."""
    feedback_old: UUID
    feedback_new: UUID
    other_feedback: UUID
    """Belongs to `other_rep` — the id a cross-reader test aims at `mark-read`."""


@pytest_asyncio.fixture(scope="session")
async def training_engine():
    engine = create_async_engine(MIGRATION_URL)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def world(training_engine) -> AsyncIterator[World]:
    """One team, two reps, three drills, a spread of graded attempts.

    Function-scoped and torn down explicitly. The isolation suite truncates every
    customer table in its own session setup, so a session-scoped world here would
    survive only while `training` happened to sort before `isolation` — an
    ordering nobody declared and pytest does not promise.
    """
    ids = {k: new_id() for k in ("org", "manager", "rep", "other")}
    suffix = str(ids["org"]).replace("-", "")[-12:]
    emails = {
        "manager": f"mgr-{suffix}@example.com",
        "rep": f"rep-{suffix}@example.com",
        "other": f"oth-{suffix}@example.com",
    }
    drills = {
        k: new_id()
        for k in ("discovery", "renewal", "self", "self_draft", "manager_draft", "archived")
    }
    assignments = {k: new_id() for k in ("renewal", "discovery")}
    feedback = {k: new_id() for k in ("old", "new", "other")}

    maker = async_sessionmaker(training_engine, expire_on_commit=False)
    async with maker() as s, s.begin():
        now = (await s.execute(text("select pg_catalog.now()"))).scalar_one()
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        month_elapsed = now - month_start

        def this_month(age_rank: int) -> datetime:
            """Keep attempts in the current database UTC month.

            Fixed offsets from the host clock can cross a month boundary. The
            database clock defines both the fixture values and the SQL readers,
            so ten elapsed-month slices retain their order without relying on
            host or session-timezone agreement.
            """
            return now - month_elapsed * (age_rank / 10)

        await s.execute(
            text("insert into org (id, name, timezone) values (:id, 'Training Org', 'UTC')"),
            {"id": ids["org"]},
        )
        # UTC deliberately: these tests assert on month and week boundaries, and a
        # shifting org timezone would make "this month" depend on the hour the
        # suite happens to run. The timezone rule itself is asserted in the
        # service by construction, not re-litigated per row here.

        async def account(key: str, *, role: str, email: str | None = None) -> None:
            await s.execute(
                text(
                    "insert into account (id, org_id, team_id, email, display_name, role,"
                    " password_hash, credential_state, status)"
                    " values (:id, :org, :team, :email, :name, :role, :hash, 'set', 'active')"
                ),
                {
                    "id": ids[key], "org": ids["org"], "team": ids["manager"],
                    "email": email or f"{key}-{suffix}@example.com",
                    "name": key.title(), "role": role, "hash": ENCODED_PASSWORD,
                },
            )

        await account("manager", role="manager", email=emails["manager"])
        await account("rep", role="rep", email=emails["rep"])
        await account("other", role="rep", email=emails["other"])

        async def drill(
            key: str, *, call_type: str, self_authored: bool, status: str = "published"
        ) -> None:
            # Two domain rules the fixture obeys rather than works around:
            #
            # `lead_type_on_discovery` is an iff — a discovery drill MUST carry a
            # lead type and a non-discovery one must NOT.
            #
            # `published_is_complete` is the publish-gate backstop: a published
            # drill cannot lack its frozen content, so scenario, answer_key, label
            # and published_at all have to be there. Seeding a published drill
            # without them would be seeding a state the product cannot produce.
            await s.execute(
                text(
                    "insert into drill (id, org_id, team_id, author_account_id, self_authored,"
                    " status, call_type, lead_type, label, scenario, answer_key, published_at)"
                    " values (:id, :org, :team, :author, :self, :status, :ct, :lead, :label,"
                    "         :scenario, :answer_key, now())"
                ),
                {
                    "id": drills[key], "org": ids["org"], "team": ids["manager"],
                    "author": ids["manager"] if not self_authored else ids["rep"],
                    "self": self_authored, "ct": call_type, "status": status,
                    "lead": "inbound_quote" if call_type == "discovery" else None,
                    "label": f"{key} drill",
                    # Versioned snapshot columns (data/04 §5), not text — they
                    # carry a `v` so a reader knows which shape it is looking at.
                    "scenario": json.dumps({"v": 1, "text": f"scenario for {key}"}),
                    "answer_key": json.dumps({"v": 1, "points": [f"point for {key}"]}),
                },
            )

        await drill("discovery", call_type="discovery", self_authored=False)
        await drill("renewal", call_type="renewal", self_authored=False)
        await drill("self", call_type="discovery", self_authored=True)
        await drill("archived", call_type="upsell", self_authored=False, status="archived")

        async def draft(key: str, *, author: UUID, self_authored: bool) -> None:
            """A draft carries no frozen content, which is the point.

            `published_is_complete` only bites at publish, so a draft legitimately
            has null scenario, answer_key, label and published_at — and the card
            has to render with a null label rather than assume one exists.
            """
            await s.execute(
                text(
                    "insert into drill (id, org_id, team_id, author_account_id,"
                    " self_authored, status, call_type, lead_type)"
                    " values (:id, :org, :team, :author, :self, 'draft', 'upsell', null)"
                ),
                {
                    "id": drills[key], "org": ids["org"], "team": ids["manager"],
                    "author": author, "self": self_authored,
                },
            )

        await draft("self_draft", author=ids["rep"], self_authored=True)
        await draft("manager_draft", author=ids["manager"], self_authored=False)

        async def assign(
            key: str,
            drill_key: str,
            *,
            used: int,
            allowed: int,
            days_out: int,
            granted_days_ago: int,
        ) -> None:
            """`granted_days_ago` is what separates the two sort orders.

            `recommended` reads `due_date`, `recently_assigned` reads `granted_at`,
            and if both assignments shared a timestamp the two orderings would
            agree by accident and neither test would be proving anything.
            """
            await s.execute(
                text(
                    "insert into assignment (id, org_id, team_id, drill_id, due_date,"
                    " attempts_allowed, created_by)"
                    # `cast(... as int)` because asyncpg sends an untyped
                    # parameter and `date + unknown` matches more than one
                    # operator, so PostgreSQL refuses to guess.
                    " values (:id, :org, :team, :drill, current_date + cast(:out as int),"
                    "         :allowed, :by)"
                ),
                {
                    "id": assignments[key], "org": ids["org"], "team": ids["manager"],
                    "drill": drills[drill_key], "out": days_out, "allowed": allowed,
                    "by": ids["manager"],
                },
            )
            await s.execute(
                text(
                    "insert into assignment_recipient (assignment_id, org_id, team_id,"
                    " rep_account_id, attempts_used, granted_at)"
                    " values (:aid, :org, :team, :rep, :used,"
                    "         now() - make_interval(days => :ago))"
                ),
                {
                    "aid": assignments[key], "org": ids["org"], "team": ids["manager"],
                    "rep": ids["rep"], "used": used, "ago": granted_days_ago,
                },
            )

        # The two orderings disagree here on purpose. Renewal is due SOONER, so
        # `recommended` leads with it; discovery was granted MORE RECENTLY, so
        # `recently_assigned` leads with that instead. A fixture where one drill
        # won both would let a sort read the wrong column and still pass.
        await assign("renewal", "renewal", used=1, allowed=3, days_out=2, granted_days_ago=10)
        await assign("discovery", "discovery", used=3, allowed=3, days_out=9, granted_days_ago=1)

        # The same assignment, granted to the OTHER rep with a fresh allowance. A
        # query that forgets `rep_account_id = :account` reads 0 of 3 here and
        # reports our rep's exhausted drill as available.
        await s.execute(
            text(
                "insert into assignment_recipient (assignment_id, org_id, team_id,"
                " rep_account_id, attempts_used) values (:aid, :org, :team, :rep, 0)"
            ),
            {
                "aid": assignments["discovery"], "org": ids["org"],
                "team": ids["manager"], "rep": ids["other"],
            },
        )

        async def graded(
            drill_key: str, *, rep: UUID, score: float, age_rank: int, self_authored: bool = False
        ) -> None:
            attempt = new_id()
            await s.execute(
                text(
                    "insert into attempt (id, org_id, team_id, drill_id, rep_account_id,"
                    " self_authored, status, started_at)"
                    " values (:id, :org, :team, :drill, :rep, :self, 'graded',"
                    "         :started_at)"
                ),
                {
                    "id": attempt, "org": ids["org"], "team": ids["manager"],
                    "drill": drills[drill_key], "rep": rep, "self": self_authored,
                    "started_at": this_month(age_rank),
                },
            )
            await s.execute(
                text(
                    "insert into scorecard (id, org_id, team_id, attempt_id, overall_score)"
                    " values (:id, :org, :team, :attempt, :score)"
                ),
                {
                    "id": new_id(), "org": ids["org"], "team": ids["manager"],
                    "attempt": attempt, "score": score,
                },
            )

        # Two discovery attempts and one renewal, all inside the trailing 30 days
        # AND inside the current month. Discovery averages 8.0 (green), renewal is
        # 6.0 (amber) — so renewal is unambiguously the weakest MEASURED type.
        await graded("discovery", rep=ids["rep"], score=7.6, age_rank=2)
        await graded("discovery", rep=ids["rep"], score=8.4, age_rank=3)
        await graded("renewal", rep=ids["rep"], score=6.0, age_rank=4)

        # The one that must never count: graded, this month, and self-authored.
        # If it leaked in, the discovery average moves and every band assertion
        # below shifts with it.
        await graded("self", rep=ids["rep"], score=2.0, age_rank=1, self_authored=True)

        # Three more on the SAME self-authored drill, making four graded attempts
        # for AC-TRP-006 — the criterion is stated as four and the arithmetic is
        # only convincing at that size.
        #
        # This drill rather than a new one, for two reasons. A new published drill
        # would enter the library and move every count assertion in
        # test_library.py; self-authored practice is excluded from the counted
        # pool, so none of the rating assertions above can move either. And it
        # puts FR-TRP-010 under direct test: private practice is excluded from
        # every statistic EXCEPT the drill's own history, which is this endpoint.
        #
        # Scores are chosen so best, latest, average and first are four different
        # numbers. Oldest to newest: 4.0, 9.0, 5.0, 2.0 — best 9.0, latest 2.0,
        # average 5.0, trend -2.0. A query that confused best with latest, or
        # ordered "first" by insertion rather than by time, matches none of them.
        await graded("self", rep=ids["rep"], score=4.0, age_rank=8, self_authored=True)
        await graded("self", rep=ids["rep"], score=9.0, age_rank=6, self_authored=True)
        await graded("self", rep=ids["rep"], score=5.0, age_rank=4, self_authored=True)

        # Ungraded, and the newest. It must appear in the attempt list with a null
        # score and must NOT reach the statistics — V-8 derives from graded
        # attempts only (FR-SCR-015).
        await s.execute(
            text(
                "insert into attempt (id, org_id, team_id, drill_id, rep_account_id,"
                " self_authored, status, started_at)"
                " values (:id, :org, :team, :drill, :rep, true, 'grading_pending', now())"
            ),
            {
                "id": new_id(), "org": ids["org"], "team": ids["manager"],
                "drill": drills["self"], "rep": ids["rep"],
            },
        )

        # The other rep, so "only my rows" is a claim with something to fail
        # against rather than a vacuous truth.
        await graded("discovery", rep=ids["other"], score=9.9, age_rank=2)

        async def feedback_item(key: str, *, rep: UUID, minutes_ago: int, read: bool) -> None:
            await s.execute(
                text(
                    "insert into coach_feedback_item (id, org_id, rep_account_id, body,"
                    " created_at, read_at)"
                    " values (:id, :org, :rep, :body,"
                    "         now() - make_interval(mins => :ago),"
                    "         case when :read then now() else null end)"
                ),
                {
                    "id": feedback[key], "org": ids["org"], "rep": rep,
                    "body": f"{key} feedback", "ago": minutes_ago, "read": read,
                },
            )

        await feedback_item("old", rep=ids["rep"], minutes_ago=60, read=False)
        await feedback_item("new", rep=ids["rep"], minutes_ago=10, read=False)
        await feedback_item("other", rep=ids["other"], minutes_ago=30, read=False)

        await s.execute(
            text(
                "insert into badge (code, name, rule_text, active) values"
                " ('first_drill', 'First Drill', 'Complete one drill', true),"
                " ('ten_drills', 'Ten Drills', 'Complete ten drills', true)"
                " on conflict (code) do nothing"
            )
        )
        await s.execute(
            text(
                "insert into badge_award (badge_code, account_id, org_id, earned_at)"
                " values ('first_drill', :rep, :org, :at)"
            ),
            {"rep": ids["rep"], "org": ids["org"], "at": now - timedelta(days=5)},
        )

    yield World(
        org=ids["org"],
        manager=ids["manager"], manager_email=emails["manager"],
        rep=ids["rep"], rep_email=emails["rep"],
        other_rep=ids["other"], other_rep_email=emails["other"],
        drill_discovery=drills["discovery"], drill_renewal=drills["renewal"],
        self_drill=drills["self"],
        self_draft=drills["self_draft"], manager_draft=drills["manager_draft"],
        archived_drill=drills["archived"],
        assignment=assignments["renewal"], locked_assignment=assignments["discovery"],
        feedback_old=feedback["old"], feedback_new=feedback["new"],
        other_feedback=feedback["other"],
    )

    async with maker() as s, s.begin():
        # `scorecard` and `attempt` are insert-only: `trg_scorecard_freeze` raises
        # on any delete, because a graded record is frozen evidence (FR-SCR-003,
        # AC-SCR-002). The ONE sanctioned way past it is the erasure context, so
        # the teardown enters it rather than reaching for a trigger disable — the
        # same door the subject-rights flow will use.
        #
        # Transaction-local, so it cannot leak into another test's session.
        await s.execute(text("select set_config('app.erasure_context', 'on', true)"))

        # Explicit dependency order. The foreign keys are NOT `on delete cascade`
        # — deleting the org first raises `fk_account_org_id` — so the teardown
        # has to know the order. That is deliberate on the schema's part: a
        # cascade on customer data is one `delete` away from an unrecoverable
        # mistake, and this fixture paying the cost is the right trade.
        for statement in (
            "delete from scorecard where org_id = :org",
            "delete from attempt where org_id = :org",
            "delete from drill where org_id = :org",
            "delete from coach_feedback_item where org_id = :org",
            "delete from badge_award where org_id = :org",
            "delete from account where org_id = :org",
            "delete from org where id = :org",
        ):
            await s.execute(text(statement), {"org": ids["org"]})

        # `badge` is deliberately NOT deleted. It is P0 reference data with no org
        # — shared with every other suite and inserted here `on conflict do
        # nothing` — so removing it would mean one suite's teardown deciding what
        # another suite's catalogue contains. Repeat runs are idempotent, and the
        # awards that reference it are org-scoped and gone above.


@dataclass(frozen=True, slots=True)
class TeamWorld:
    """Five rated reps — the smallest team FR-TRM-003 will tier at all.

    Separate from `World` rather than grown out of it. `World` seeds two reps and
    every `/me` test asserts literal scores against them; taking it to five would
    rewrite the numbers those tests were written around, for the benefit of a
    surface they never touch.
    """

    org: UUID
    manager: UUID
    manager_email: str
    team: UUID
    """Equal to `manager`. `ck_account_team_is_owning_manager` requires a
    manager's team to be their own id, and every rep's to be their manager's."""

    reps: tuple[UUID, ...]
    """The five, in DESCENDING rating order.

    `reps[0]` rates 8.1 and is the sole top performer; `reps[4]` rates 6.5 and is
    the sole needs-coaching. The ratings are deliberately distinct, so V-4's
    `rep_account_id` tie-break never engages and the ranks stay a property of the
    scores rather than of whichever ids the fixture happened to mint that run."""

    rep_emails: tuple[str, ...]

    deactivated_rep: UUID
    """On the team, deactivated, holding no attempts.

    FR-IDA-010 refuses their authentication and preserves their records; it does
    not remove them from the team, and the derivations apply no status filter. So
    the roster carries no status filter either, and this rep is what holds that
    line — without them, re-adding `status = 'active'` would fail nothing."""

    unrated_rep: UUID
    """A sixth rep on the same team, holding NO attempts at all.

    FR-TRM-005's roster is "every team rep", and this one has no row in V-2 and
    therefore none in V-4 — so a roster read off the tier view drops them while
    every assertion about the other five still passes.

    They change nothing else: `rated_reps` counts reps with counted attempts, so
    it stays five, and the tiers, team average and gaps are untouched."""

    drill_discovery: UUID
    drill_post_proposal: UUID
    drill_renewal: UUID
    drill_upsell: UUID
    """Attempted by `reps[2]` alone — a MID rep, in neither cohort. So upsell has
    a gap row whose two cohort averages are both null, which is the only way to
    observe that a null gap sorts LAST rather than first."""

    self_drill: UUID
    """Authored by `reps[0]`. Their graded 2.0 on it must reach no rating."""

    other_manager: UUID
    other_manager_email: str
    other_rep: UUID
    other_drill: UUID
    """The second team's drill. Absent from the first manager's catalog, and the
    only id that can demonstrate it — the self-authored one proves concealment,
    which is a different rule."""
    """A SECOND manager and rep in the SAME org, on their own team.

    AC-TRM-006 is "given two managers in one org", and same-org is the case worth
    building: a different org is also a different `app.org_id`, so it would be
    caught by the org predicate long before the team one — and the team predicate
    is the thing this surface actually depends on.

    Their single attempt scores 1.0, far below anything on the main team, so a
    dropped team filter cannot be subtle: the team average falls from 7.2 to 6.2
    and `rated_reps` goes from five to six."""


TEAM_SCORES: tuple[tuple[float, float, float], ...] = (
    (9.0, 8.2, 7.0),  # mean 8.0667 -> 8.1  rank 1  top
    (7.8, 7.6, 7.4),  # mean 7.6    -> 7.6  rank 2  mid
    (7.2, 7.0, 6.9),  # mean 7.0333 -> 7.0  rank 3  mid
    (6.8, 6.9, 6.5),  # mean 6.7333 -> 6.7  rank 4  mid
    (6.6, 6.7, 6.3),  # mean 6.5333 -> 6.5  rank 5  needs_coaching
)
"""Discovery, post-proposal, renewal — one graded attempt each, per rep.

Chosen so three separate things are checkable at once rather than seeded three
times over:

**The rounding bites.** Rep 1's mean is 8.0667 and rep 3's is 7.0333, so a view
that truncated instead of rounding disagrees rather than coinciding.

**The team average is not the attempt average.** Member ratings mean 7.18 -> 7.2;
the raw scores mean 7.1867. FR-TRM-002 asks for the former, and these differ
enough to tell the two implementations apart.

**The gaps land on AC-TRM-002's worked example.** Top cohort is rep 1; the bottom
half is ranks 4 and 5. Per call type that is 9.0 vs 6.7, 8.2 vs 6.8, and 7.0 vs
6.4 — gaps of 2.3, 1.4 and 0.6, which the criterion marks high, moderate and
unmarked. One fixture therefore serves AC-TRM-001 and AC-TRM-002 both.

No score is 7.5 or 5.5. Bands are asserted elsewhere and a seeded value sitting
on a cut would make this fixture's arithmetic hostage to a rounding tie.
"""

_ATTEMPT_AT = (
    "(date_trunc('month', now() at time zone 'UTC')"
    " + make_interval(months => :months, days => :day, hours => :hour)) at time zone 'UTC'"
)
"""Anchored to the month, and to the month **as V-1 buckets it**.

Two decisions, each one load-bearing.

**Anchored to the month, not to `now() - N days`.** Every number here is a
calendar-month aggregate, and a days-ago offset run on the 2nd of a month lands
in the previous one — the fixture would seed a month the assertions do not read,
and the file would fail for a reason unrelated to the code.

**Anchored in UTC, explicitly, both ways.** `date_trunc('month', now())` would
truncate in the SESSION timezone, which is not the org's: this database reports
`Africa/Cairo`, so on the last evening of a month the session is already in the
next one while UTC is not. V-1 buckets on `started_at at time zone o.timezone`
and the org here is UTC, so the seed has to agree with that and nothing else. The
trailing `at time zone 'UTC'` converts the naive result back to `timestamptz`
without the session zone reinterpreting it — omit it and the value shifts by the
session offset on the way into the column, which near a boundary moves the row
into the previous month.

Offsets are per-attempt rather than per-rep, so every row gets a distinct
timestamp. An earlier version clamped with `least(now(), ...)`, which collapsed
all fifteen onto the same instant whenever the suite ran on the 1st — harmless
for a month aggregate, and quietly fatal for any later assertion that orders a
rep's attempts. Days 0-4 plus a few hours keeps every row inside the month; on
the 1st some are dated slightly ahead of `now()`, which no aggregate here can
observe.
"""


@pytest_asyncio.fixture
async def team_world(training_engine) -> AsyncIterator[TeamWorld]:
    """One manager, five rated reps, and the three things counted nowhere."""
    org = new_id()
    manager = new_id()
    reps = tuple(new_id() for _ in range(5))
    suffix = str(org).replace("-", "")[-12:]
    manager_email = f"tm-mgr-{suffix}@example.com"
    other_manager_email = f"tm-omgr-{suffix}@example.com"
    rep_emails = tuple(f"tm-rep{i}-{suffix}@example.com" for i in range(5))
    unrated_rep = new_id()
    deactivated_rep = new_id()
    other_manager = new_id()
    other_rep = new_id()
    drills = {
        k: new_id()
        for k in ("discovery", "post_proposal", "renewal", "upsell", "self", "other")
    }

    maker = async_sessionmaker(training_engine, expire_on_commit=False)
    async with maker() as s, s.begin():
        await s.execute(
            text("insert into org (id, name, timezone) values (:id, 'Team Org', 'UTC')"),
            {"id": org},
        )
        # UTC for the same reason `world` uses it: these assertions are about
        # which calendar month an attempt fell in, and a shifting org timezone
        # would make that depend on the hour the suite happened to run. That the
        # bucketing IS org-local is asserted where the anchor is resolved, not
        # re-litigated per row here.

        async def account(
            account_id: UUID,
            *,
            role: str,
            email: str,
            name: str,
            team: UUID | None = None,
            status: str = "active",
        ) -> None:
            await s.execute(
                text(
                    "insert into account (id, org_id, team_id, email, display_name, role,"
                    " password_hash, credential_state, status)"
                    " values (:id, :org, :team, :email, :name, :role, :hash, 'set', :status)"
                ),
                {
                    "status": status,
                    "id": account_id, "org": org,
                    # The manager owns the team and reps point at it —
                    # `ck_account_team_is_owning_manager` requires exactly that.
                    "team": team or manager,
                    "email": email, "name": name, "role": role, "hash": ENCODED_PASSWORD,
                },
            )

        await account(manager, role="manager", email=manager_email, name="Team Manager")
        for index, (rep, email) in enumerate(zip(reps, rep_emails, strict=True)):
            await account(rep, role="rep", email=email, name=f"Rep {index + 1}")

        # On the team, never measured. The roster must still name them.
        await account(
            unrated_rep,
            role="rep",
            email=f"tm-unrated-{suffix}@example.com",
            name="Unrated Rep",
        )

        # Deactivated, and still on the roster. FR-IDA-010 refuses their
        # authentication and preserves their records; it does not take them off
        # the team. The derivations apply no status filter either, so a roster
        # that did would report a different population from the dashboard sitting
        # directly above it.
        #
        # Seeded with NO attempts deliberately. A deactivated rep who HAD attempts
        # would make `rated_reps` six, which moves the tier split, the bottom-half
        # cohort and therefore every gap — a large recomputation to test a
        # predicate that is absent. This proves the absence; the harder
        # consistency case is noted in `test_team_roster.py`.
        await account(
            deactivated_rep,
            role="rep",
            email=f"tm-deact-{suffix}@example.com",
            name="Deactivated Rep",
            status="deactivated",
        )

        async def drill(
            key: str,
            *,
            call_type: str,
            author: UUID,
            self_authored: bool,
            team: UUID | None = None,
        ) -> None:
            await s.execute(
                text(
                    "insert into drill (id, org_id, team_id, author_account_id, self_authored,"
                    " status, call_type, lead_type, label, scenario, answer_key, published_at)"
                    " values (:id, :org, :team, :author, :self, 'published', :ct, :lead, :label,"
                    "         :scenario, :answer_key, now())"
                ),
                {
                    "id": drills[key], "org": org, "team": team or manager, "author": author,
                    "self": self_authored, "ct": call_type,
                    # `lead_type_on_discovery` is an iff, not a default.
                    "lead": "inbound_quote" if call_type == "discovery" else None,
                    "label": f"team {key}",
                    "scenario": json.dumps({"v": 1, "text": f"scenario for {key}"}),
                    "answer_key": json.dumps({"v": 1, "points": [f"point for {key}"]}),
                },
            )

        await drill("discovery", call_type="discovery", author=manager, self_authored=False)
        await drill("post_proposal", call_type="post_proposal", author=manager, self_authored=False)
        await drill("renewal", call_type="renewal", author=manager, self_authored=False)
        await drill("upsell", call_type="upsell", author=manager, self_authored=False)
        # Authored by the top performer, so their private practice below has
        # somewhere to live. `self_authored` must agree with the attempt's own
        # flag: the foreign key is composite on (drill_id, org_id, self_authored).
        await drill("self", call_type="discovery", author=reps[0], self_authored=True)

        async def graded(
            drill_key: str,
            *,
            rep: UUID,
            score: float,
            day: int,
            hour: int,
            months: int = 0,
            self_authored: bool = False,
            team: UUID | None = None,
        ) -> None:
            attempt = new_id()
            await s.execute(
                text(
                    "insert into attempt (id, org_id, team_id, drill_id, rep_account_id,"
                    " self_authored, status, started_at)"
                    f" values (:id, :org, :team, :drill, :rep, :self, 'graded', {_ATTEMPT_AT})"
                ),
                {
                    "id": attempt, "org": org, "team": team or manager,
                    "drill": drills[drill_key],
                    "rep": rep, "self": self_authored,
                    "months": months, "day": day, "hour": hour,
                },
            )
            # `ended_at` stays null. The row needs no end time to be graded, and
            # supplying one would mean choosing a clock — `ck_attempt_ends_after_start`
            # compares it against a postgres-stamped `started_at`, and a
            # host-side value there is what made T-2 flaky.
            await s.execute(
                text(
                    "insert into scorecard (id, org_id, team_id, attempt_id, overall_score)"
                    " values (:id, :org, :team, :attempt, :score)"
                ),
                {
                    "id": new_id(), "org": org, "team": team or manager,
                    "attempt": attempt, "score": score,
                },
            )

        # One day per rep, one hour per call type, so all fifteen timestamps are
        # distinct and a rep's three attempts have a defined order.
        for index, (rep, scores) in enumerate(zip(reps, TEAM_SCORES, strict=True)):
            discovery, post_proposal, renewal = scores
            await graded("discovery", rep=rep, score=discovery, day=index, hour=1)
            await graded("post_proposal", rep=rep, score=post_proposal, day=index, hour=2)
            await graded("renewal", rep=rep, score=renewal, day=index, hour=3)

        # Upsell, attempted by ONE mid rep and nobody else.
        #
        # `reps[2]` ranks 3rd — neither top nor bottom half — so V-6 emits an
        # upsell row whose two cohort averages are both null, and therefore a null
        # gap. That is the only shape that can show a null gap sorting LAST:
        # PostgreSQL puts NULLs FIRST under `desc`, so without `nulls last` the one
        # row carrying no finding would head a list ordered worst-first.
        #
        # 7.0 is chosen to leave every other number alone. It moves this rep's mean
        # from 7.0333 to 7.025, which still rounds to 7.0 — so their rating, their
        # rank, the tier split, the team average and all three real gaps are
        # untouched. Only their `counted_attempts` moves, 3 to 4.
        await graded("upsell", rep=reps[2], score=7.0, day=2, hour=4)

        # The PRIOR month, so the dashboard's delta has something to subtract.
        #
        # Every rep scores 6.0 exactly once, which makes the prior team average
        # 6.0 and the delta a clean 7.2 - 6.0 = 1.2. Without this the `left join`
        # onto `cur.month - interval '1 month'` is never exercised: a join written
        # `+` instead of `-`, or onto the wrong column, returns null either way and
        # every assertion about the current month still passes.
        #
        # A flat prior month is deliberate. Varying it would also move the prior
        # month's own tiers and gaps, and nothing asserts those — so the extra
        # data would be unverified noise sitting inside a fixture whose value is
        # that every row in it is accounted for.
        #
        # FOUR reps, not five, and that is the second thing this month buys.
        # FR-TRM-003 suppresses tiering "while fewer than five reps hold counted
        # attempts", so the boundary is 4 against 5 — and testing 5 against some
        # smaller number would leave `< 3` or `< 4` passing just as well. The prior
        # month is the four-rep side; the current month is the five-rep side.
        #
        # It costs the delta nothing: the mean of four 6.0s is still 6.0.
        for index, rep in enumerate(reps[:4]):
            await graded("discovery", rep=rep, score=6.0, day=index, hour=1, months=-1)

        # Exclusion 1 — self-authored practice (FR-TRP-010), on the TOP performer.
        # Counted, 2.0 drags their rating from 8.1 to 6.6 and costs them rank 1,
        # which scrambles the tiers, moves the team average, and changes every gap
        # row because the top cohort IS this rep. The loudest placement available.
        await graded("self", rep=reps[0], score=2.0, day=0, hour=4, self_authored=True)

        # Exclusion 2 — a non-graded attempt (FR-SCR-014), with no scorecard.
        # V-1 refuses it twice: on `status = 'graded'` and on the inner join to
        # `scorecard`. Seeded on rep 2 so a leak would move a MID rep, which is a
        # different failure signature from the self-authored one above.
        await s.execute(
            text(
                "insert into attempt (id, org_id, team_id, drill_id, rep_account_id,"
                " self_authored, status, started_at)"
                f" values (:id, :org, :team, :drill, :rep, false, 'interrupted', {_ATTEMPT_AT})"
            ),
            {
                "id": new_id(), "org": org, "team": manager,
                "drill": drills["discovery"], "rep": reps[1],
                "months": 0, "day": 1, "hour": 4,
            },
        )

        # A SECOND team, in the SAME org — AC-TRM-006's "two managers in one org".
        #
        # Nothing of this team may reach the first manager's dashboard. The single
        # attempt scores 1.0, so a dropped `team_id` predicate is not a subtle
        # failure: the first team's average falls 7.2 -> 6.2 and `rated_reps` goes
        # 5 -> 6. Both are asserted, because a leak has to move both and a test
        # watching only the average could be satisfied by a compensating error.
        #
        # Same org deliberately. A second ORG would also carry a different
        # `app.org_id`, so the org predicate would catch a leak long before the
        # team predicate was ever exercised — and the team predicate is what this
        # surface actually rests on.
        # `team=other_manager`, not the default: a manager's team IS their own id
        # (`ck_account_team_is_owning_manager`), so the second manager owns a
        # second team rather than joining the first.
        await account(
            other_manager,
            role="manager",
            email=other_manager_email,
            name="Other Manager",
            team=other_manager,
        )
        await account(
            other_rep,
            role="rep",
            email=f"tm-orep-{suffix}@example.com",
            name="Other Rep",
            team=other_manager,
        )
        await drill(
            "other",
            call_type="discovery",
            author=other_manager,
            self_authored=False,
            team=other_manager,
        )
        await graded(
            "other", rep=other_rep, score=1.0, day=0, hour=1, team=other_manager
        )

        # Exclusion 3 — a test call — is seeded as nothing, and that is the
        # correct seeding. FR-DRL-012: a test call "leaves no attempt record, no
        # review, and no trace in any statistic", and `attempt` carries no flag
        # for one. The exclusion is structural rather than a predicate, so there
        # is no row to write and none a regression could re-admit.

    yield TeamWorld(
        org=org,
        manager=manager,
        manager_email=manager_email,
        team=manager,
        reps=reps,
        rep_emails=rep_emails,
        unrated_rep=unrated_rep,
        deactivated_rep=deactivated_rep,
        drill_discovery=drills["discovery"],
        drill_post_proposal=drills["post_proposal"],
        drill_renewal=drills["renewal"],
        drill_upsell=drills["upsell"],
        self_drill=drills["self"],
        other_manager=other_manager,
        other_manager_email=other_manager_email,
        other_rep=other_rep,
        other_drill=drills["other"],
    )

    async with maker() as s, s.begin():
        # `trg_scorecard_freeze` raises on any delete of graded evidence
        # (FR-SCR-003). The erasure context is the one sanctioned door past it,
        # and it is transaction-local so it cannot leak into another test.
        await s.execute(text("select set_config('app.erasure_context', 'on', true)"))
        for statement in (
            "delete from scorecard where org_id = :org",
            "delete from attempt where org_id = :org",
            "delete from drill where org_id = :org",
            "delete from account where org_id = :org",
            "delete from org where id = :org",
        ):
            await s.execute(text(statement), {"org": org})


@pytest_asyncio.fixture
async def app(world) -> AsyncIterator[FastAPI]:
    """The real app with a fake Valkey.

    Separate from `client` so a test can reach the application itself — to mount a
    probe route for a dependency whose real routes do not exist yet, which is how
    `test_team_gate.py` exercises the manager gate. Anything mounted here is
    per-test and never reaches the surface `check_conformance_diff.py` enumerates.
    """
    from bluelab.entrypoints.api import create_app
    from bluelab.platform.config import get_settings

    settings = app_settings()
    application = create_app(settings)
    application.dependency_overrides[get_settings] = lambda: settings
    application.state.valkey = fakeredis.aioredis.FakeRedis(decode_responses=True)

    yield application

    await application.state.valkey.aclose()


@pytest_asyncio.fixture
async def client(app) -> AsyncIterator[AsyncClient]:
    """A client onto that app.

    A fresh instance per test, so a session opened by one test cannot authenticate
    another.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://api.test") as http:
        yield http


@pytest.fixture
def org_month(training_engine):
    """`YYYY-MM` as the ORG's calendar has it, read from the DATABASE.

    Not `datetime.now(UTC).strftime(...)`. The server resolves the month from
    postgres, so deriving the expectation from the host clock would make every
    month assertion depend on two machines agreeing at a boundary — the mistake
    that made T-2 flaky.

    `months_back` steps whole civil months, so the month before March is February
    rather than "thirty days ago".

    Lives here because three suites need it — the dashboard, the roster and the
    deep dive all echo a month back — and three copies of a query is three places
    for the timezone to be got wrong.
    """
    maker = async_sessionmaker(training_engine, expire_on_commit=False)

    async def _month(*, months_back: int = 0) -> str:
        async with maker() as session:
            return (
                await session.execute(
                    text(
                        "select to_char(date_trunc('month', now() at time zone 'UTC')"
                        " - make_interval(months => :back), 'YYYY-MM')"
                    ),
                    {"back": months_back},
                )
            ).scalar_one()

    return _month


@pytest.fixture
def sign_in(client, world):
    """Authenticate as one of the seeded reps and keep the cookie on the client."""

    async def _as(email: str | None = None) -> None:
        response = await client.post(
            "/api/v1/auth/session",
            json={"email": email or world.rep_email, "password": PASSWORD},
        )
        assert response.status_code == 200, response.text

    return _as
