"""Fixtures for the freeze-guard attack battery.

These run as the **migration role**, deliberately. The guards under test are
triggers, not policies: connecting as the constrained app role would mean a
refusal could come from RLS rather than from the trigger, and a test that cannot
tell which mechanism refused it is not evidence about either.

`BYPASSRLS` removes RLS from the picture entirely, so every refusal here is the
trigger — which is the only way "the freeze guard holds" becomes a claim about
the freeze guard.

Each test gets a **fresh drill or position**, because these tests mutate state by
design and a shared fixture would make them order-dependent (quality/01 §4).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from tests.support import required_url

from bluelab.platform.ids import new_id

MIGRATION_URL = required_url("TEST_MIGRATION_URL")


@pytest_asyncio.fixture(scope="session")
async def engine():
    eng = create_async_engine(MIGRATION_URL)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture(scope="session")
async def base_org(engine) -> dict[str, UUID]:
    """One org and one manager, reused. Neither is ever mutated by these tests."""
    ids = {"org": new_id(), "manager": new_id()}
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s, s.begin():
        await s.execute(
            text("insert into org (id, name, timezone) values (:id, 'Freeze', 'Africa/Cairo')"),
            {"id": ids["org"]},
        )
        await s.execute(
            text(
                "insert into account (id, org_id, team_id, email, display_name, role, password_hash)"
                " values (:id, :org, :id, :email, 'Mgr', 'manager', 'x')"
            ),
            {"id": ids["manager"], "org": ids["org"], "email": f"freeze-{ids['manager']}@t.test"},
        )
    return ids


@pytest_asyncio.fixture
async def session(engine) -> AsyncIterator[AsyncSession]:
    """A per-test session. Each test owns its own transaction boundaries because
    a trigger refusal aborts the transaction it fires in."""
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s


@pytest.fixture
def make_drill(session, base_org):
    """Create a drill in a given status, committed and ready to attack."""

    async def _make(*, status: str = "published", self_authored: bool = False) -> UUID:
        drill_id = new_id()
        frozen = status in ("published", "archived")
        async with session.begin():
            await session.execute(
                text(
                    "insert into drill (id, org_id, team_id, author_account_id, self_authored,"
                    " status, call_type, lead_type, label, scenario, answer_key, published_at)"
                    " values (:id, :org, :team, :team, :sa, :status, 'discovery', 'referral',"
                    "         :label, :scen, :ak, case when :frozen then pg_catalog.now() end)"
                ),
                {
                    "id": drill_id, "org": base_org["org"], "team": base_org["manager"],
                    "sa": self_authored, "status": status,
                    "label": "Buyer" if frozen else None,
                    "scen": '{"v": 1}' if frozen else None,
                    "ak": '{"v": 1}' if frozen else None,
                    "frozen": frozen,
                },
            )
            await session.execute(
                text(
                    "insert into drill_concealed (drill_id, org_id, team_id, challenges, hidden_motives)"
                    " values (:id, :org, :team, '[]', '[]')"
                ),
                {"id": drill_id, "org": base_org["org"], "team": base_org["manager"]},
            )
        return drill_id

    return _make


@pytest.fixture
def make_position(session, base_org):
    """Create a position, optionally already frozen by a first invite (T-7)."""

    async def _make(*, frozen: bool = False) -> UUID:
        position_id = new_id()
        async with session.begin():
            await session.execute(
                text(
                    "insert into position (id, org_id, team_id, title, openings, status,"
                    " assessment_frozen_at)"
                    " values (:id, :org, :team, 'AE', 1, 'active',"
                    "         case when :frozen then pg_catalog.now() end)"
                ),
                {
                    "id": position_id, "org": base_org["org"], "team": base_org["manager"],
                    "frozen": frozen,
                },
            )
        return position_id

    return _make
