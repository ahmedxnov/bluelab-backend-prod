"""Fixtures for the isolation suite — the Phase-13 entry gate (data FS-1).

**No feature migration merges until this suite passes against *generated*
policies.** It is the reason the policy generator exists: five principal kinds
across 38 tables is ~170 decisions, and the only way to trust them is to attack
one mechanism rather than review thirty-eight.

## The world these fixtures build

Two orgs, because a single-org fixture cannot fail a cross-tenant test — it has
nothing to leak *to*. Two teams inside the first org, because the team boundary
(FR-IDA-009) is a different boundary from the org one (CMP-004) and a test that
conflates them proves neither.

    org A ──┬── team 1: manager M1, reps R1a, R1b
            └── team 2: manager M2, rep  R2a
    org B ───── team 3: manager M3, rep  R3a

    M1 owns: a published team drill, a draft, a position with a candidate
    R1a owns: a SELF-AUTHORED drill — invisible even to M1 (AC-TRP-004)

## How the tests connect

Every test runs through `as_principal(...)`, which opens a transaction with the
scope GUCs applied exactly as a request would — the same `apply_scope` the
application uses, not a test-only shortcut. A suite that set the GUCs its own way
would be testing its own helper.

Seeding runs as the **migration role** (`BYPASSRLS`), because the world has to
exist before any principal can be denied a view of it.

## Why the app role must be non-superuser

`assert_app_role_is_constrained` runs first, and fails loudly if the connection
role holds `BYPASSRLS` or superuser. Without that check, every isolation test in
this file would pass on a database where RLS was never enforced at all — a green
suite proving nothing. That is SEC-041's false-green, and it is the single most
dangerous outcome available to this suite.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from tests.support import required_url

from bluelab.platform.db.privileged import system_scope
from bluelab.platform.db.scope import Role, ScopeContext, apply_scope
from bluelab.platform.ids import new_id

APP_URL = required_url("TEST_DATABASE_URL")
MIGRATION_URL = required_url("TEST_MIGRATION_URL")


@dataclass(frozen=True, slots=True)
class World:
    """Ids of the seeded fixture world. Deliberately a flat bag of ids.

    Nothing here holds an ORM object: an object carries a session identity, and a
    test that reads an attribute off a detached instance can pass without the
    database ever having been asked.
    """

    org_a: UUID
    org_b: UUID
    m1: UUID
    m2: UUID
    m3: UUID
    r1a: UUID
    r1b: UUID
    r2a: UUID
    r3a: UUID
    published_drill: UUID
    draft_drill: UUID
    self_authored_drill: UUID
    assignment: UUID
    """Granted to `r1a` and to r1a ALONE.

    `r1b` is on the same team under the same manager and is deliberately NOT a
    recipient, which is what makes the peer-denial assertions mean something: the
    only thing separating the two is the `assignment_recipient` row.
    """
    position: UUID
    candidate: UUID
    candidate_attempt: UUID
    rep_attempt: UUID
    scorecard: UUID


@pytest_asyncio.fixture(scope="session")
async def migration_engine():
    engine = create_async_engine(MIGRATION_URL, poolclass=None)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(scope="session")
async def app_engine():
    """The application role — non-superuser, no BYPASSRLS. What production uses."""
    engine = create_async_engine(APP_URL)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(scope="session", autouse=True)
async def assert_app_role_is_constrained(app_engine) -> None:
    """Fail the whole suite if RLS could not possibly be enforced.

    This runs before anything else, and it is not a formality. If the connection
    role is superuser or holds `BYPASSRLS`, every isolation assertion below still
    passes — because there are no policies being applied to defeat. A green suite
    on an unenforced database is worse than a red one (SEC-041, pipeline/02 §3).
    """
    async with app_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "select rolsuper, rolbypassrls, "
                    "has_schema_privilege(current_user, 'public', 'CREATE') as can_create "
                    "from pg_roles where rolname = current_user"
                )
            )
        ).one()

    assert not row.rolsuper, (
        "the test app role is SUPERUSER — RLS is not enforced against it, so every "
        "isolation assertion in this suite would pass vacuously (SEC-041)"
    )
    assert not row.rolbypassrls, (
        "the test app role holds BYPASSRLS — same false green as superuser"
    )
    assert not row.can_create, (
        "the test app role can CREATE in schema public — it could shadow a name a "
        "SECURITY DEFINER helper resolves, and those helpers run with BYPASSRLS "
        "(CWE-426, sql/roles/bootstrap.sql)"
    )


@pytest_asyncio.fixture(scope="session")
async def world(migration_engine) -> World:
    """Seed two orgs, three teams, and one of everything worth denying."""
    ids = {name: new_id() for name in (
        "org_a", "org_b", "m1", "m2", "m3", "r1a", "r1b", "r2a", "r3a",
        "published_drill", "draft_drill", "self_authored_drill", "assignment",
        "position", "candidate", "candidate_attempt", "rep_attempt", "scorecard",
    )}

    maker = async_sessionmaker(migration_engine, expire_on_commit=False)
    async with maker() as session, session.begin():
        await _reset(session)
        await _seed(session, ids)

    return World(**ids)


async def _reset(session: AsyncSession) -> None:
    """Empty every customer table before seeding.

    Order-independence is mandatory and each run seeds its own data
    (quality/01 §4) — but the database outlives the process, so "its own data"
    has to mean *only* its own. Without this the second run collides on
    `uq_account_email` and every test errors in setup, which is exactly what the
    first real run did.

    One `TRUNCATE ... CASCADE` rather than per-table deletes: it ignores foreign
    keys, so the fixture does not have to know the dependency order, and it
    cannot half-succeed. `alembic_version` is excluded — dropping the lineage
    marker would make the next `upgrade head` try to re-apply the initial
    migration onto a populated schema.
    """
    rows = await session.execute(
        text(
            "select tablename from pg_tables "
            "where schemaname = 'public' and tablename not in ('alembic_version') "
            "and tablename not like 'procrastinate_%'"
        )
    )
    tables = [r[0] for r in rows]
    if tables:
        await session.execute(
            text("truncate table " + ", ".join(tables) + " restart identity cascade")
        )


async def _seed(session: AsyncSession, i: dict[str, UUID]) -> None:
    """Raw SQL, on purpose.

    Seeding through the ORM would exercise the scoped-session factory — the very
    layer whose second-belt behaviour some of these tests want to bypass so they
    can prove RLS holds *on its own*. Raw inserts as the migration role put the
    world in place without any application-layer opinion about it.
    """
    async def ex(sql: str, **params: object) -> None:
        await session.execute(text(sql), params)

    for org, name in ((i["org_a"], "Org A"), (i["org_b"], "Org B")):
        await ex("insert into org (id, name, timezone) values (:id, :n, 'Africa/Cairo')", id=org, n=name)

    async def account(aid: UUID, org: UUID, team: UUID, role: str, email: str) -> None:
        await ex(
            "insert into account (id, org_id, team_id, email, display_name, role, password_hash)"
            " values (:id, :org, :team, :email, :email, :role, 'x')",
            id=aid, org=org, team=team, email=email, role=role,
        )

    # Managers are their own team (FR-IDA-009), so they must exist first.
    await account(i["m1"], i["org_a"], i["m1"], "manager", "m1@a.test")
    await account(i["m2"], i["org_a"], i["m2"], "manager", "m2@a.test")
    await account(i["m3"], i["org_b"], i["m3"], "manager", "m3@b.test")
    await account(i["r1a"], i["org_a"], i["m1"], "rep", "r1a@a.test")
    await account(i["r1b"], i["org_a"], i["m1"], "rep", "r1b@a.test")
    await account(i["r2a"], i["org_a"], i["m2"], "rep", "r2a@a.test")
    await account(i["r3a"], i["org_b"], i["m3"], "rep", "r3a@b.test")

    async def drill(did: UUID, org: UUID, team: UUID, author: UUID, *, self_authored: bool, status: str) -> None:
        frozen = status in ("published", "archived")
        await ex(
            "insert into drill (id, org_id, team_id, author_account_id, self_authored, status,"
            " call_type, lead_type, label, scenario, answer_key, published_at)"
            " values (:id, :org, :team, :author, :sa, :status, 'discovery', 'referral',"
            "         :label, :scen, :ak, :pub)",
            id=did, org=org, team=team, author=author, sa=self_authored, status=status,
            label="A buyer" if frozen else None,
            scen='{"v": 1}' if frozen else None,
            ak='{"v": 1}' if frozen else None,
            # A real datetime, not the string "now()" — the driver binds this as a
            # timestamptz parameter and will not parse SQL out of a value.
            pub=datetime.now(UTC) if frozen else None,
        )
        await ex(
            "insert into drill_concealed (drill_id, org_id, team_id, challenges, hidden_motives)"
            " values (:id, :org, :team, '[]', '[]')",
            id=did, org=org, team=team,
        )

    await drill(i["published_drill"], i["org_a"], i["m1"], i["m1"], self_authored=False, status="published")
    await drill(i["draft_drill"], i["org_a"], i["m1"], i["m1"], self_authored=False, status="draft")
    await drill(i["self_authored_drill"], i["org_a"], i["m1"], i["r1a"], self_authored=True, status="published")

    # One assignment on the published drill, granted to r1a and NOT to r1b.
    # `due_date` and `attempts_allowed` live here rather than on the recipient row,
    # which is the whole reason a rep needs a read on this table at all — and the
    # reason the read has to stop at membership.
    await ex(
        "insert into assignment (id, org_id, team_id, drill_id, due_date, attempts_allowed,"
        " created_by) values (:id, :org, :team, :drill, current_date + 7, 3, :by)",
        id=i["assignment"], org=i["org_a"], team=i["m1"],
        drill=i["published_drill"], by=i["m1"],
    )
    await ex(
        "insert into assignment_recipient (assignment_id, org_id, team_id, rep_account_id,"
        " attempts_used) values (:aid, :org, :team, :rep, 1)",
        aid=i["assignment"], org=i["org_a"], team=i["m1"], rep=i["r1a"],
    )

    await ex(
        "insert into position (id, org_id, team_id, title, openings, status)"
        " values (:id, :org, :team, 'AE', 1, 'active')",
        id=i["position"], org=i["org_a"], team=i["m1"],
    )
    await ex(
        "insert into candidate (id, org_id, team_id, position_id, name, email)"
        " values (:id, :org, :team, :pos, 'Cand', 'c@a.test')",
        id=i["candidate"], org=i["org_a"], team=i["m1"], pos=i["position"],
    )

    await ex(
        "insert into attempt (id, org_id, team_id, drill_id, rep_account_id, self_authored, status)"
        " values (:id, :org, :team, :drill, :rep, false, 'graded')",
        id=i["rep_attempt"], org=i["org_a"], team=i["m1"],
        drill=i["published_drill"], rep=i["r1a"],
    )
    await ex(
        "insert into scorecard (id, org_id, team_id, attempt_id, overall_score)"
        " values (:id, :org, :team, :attempt, 7.5)",
        id=i["scorecard"], org=i["org_a"], team=i["m1"], attempt=i["rep_attempt"],
    )


@pytest.fixture
def as_principal(app_engine):
    """Open a scoped transaction as any principal — the production path.

    Uses the same `apply_scope` a request uses. A helper that set the GUCs its own
    way would be testing the helper.
    """
    maker = async_sessionmaker(app_engine, expire_on_commit=False)

    def _factory(scope: ScopeContext):
        class _Ctx:
            async def __aenter__(self) -> AsyncSession:
                self._session = maker()
                await self._session.__aenter__()
                self._tx = self._session.begin()
                await self._tx.__aenter__()
                await apply_scope(self._session, scope)
                return self._session

            async def __aexit__(self, *exc: object) -> None:
                await self._tx.__aexit__(*exc)
                await self._session.__aexit__(*exc)

        return _Ctx()

    return _factory


@pytest.fixture
def manager(world):
    def _scope(account_id: UUID, org_id: UUID) -> ScopeContext:
        return ScopeContext.account(
            org_id=org_id, team_id=account_id, account_id=account_id, role=Role.MANAGER
        )

    return _scope


@pytest.fixture
def rep(world):
    def _scope(account_id: UUID, team_id: UUID, org_id: UUID) -> ScopeContext:
        return ScopeContext.account(
            org_id=org_id, team_id=team_id, account_id=account_id, role=Role.REP
        )

    return _scope


@pytest.fixture
def candidate(world):
    def _scope() -> ScopeContext:
        return ScopeContext.candidate(
            org_id=world.org_a,
            team_id=world.m1,
            position_id=world.position,
            candidate_id=world.candidate,
        )

    return _scope


@pytest.fixture
def system(world):
    return lambda org_id=None: system_scope(org_id=org_id or world.org_a)
