"""Cross-org and cross-team isolation — CMP-004 and FR-IDA-009.

The two boundaries are tested separately because they are separate claims. Org
isolation is the compliance boundary (CMP-004); team isolation is a functional
one inside a single tenant (FR-IDA-009, AC-IDA-007). A suite that only proved the
first would leave one customer's managers reading each other's coaching data.

**These assert on the derived views as well as the base tables** (data FS-1).
A `security_invoker = false` view owned by the migration role would run with
BYPASSRLS and return every org's rows through what looks like a read-only
aggregate — base-table tests would stay green throughout.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

pytestmark = [pytest.mark.l3_integration, pytest.mark.invariant_path]

CUSTOMER_TABLES = [
    "account", "drill", "drill_concealed", "rubric_dimension",
    "product_document", "fact_set", "product_fact", "document_upload",
    "attempt", "transcript_entry", "scorecard", "dimension_score", "moment",
    "position", "assessment_stage", "candidate", "candidate_report",
    "shortlist", "shortlist_candidate", "candidate_token", "email_send",
    "coach_feedback_item", "badge_award", "assignment", "assignment_recipient",
]

DERIVED_VIEWS = [
    "v_counted_attempt", "v_rep_monthly_rating", "v_rep_month_trend",
    "v_rep_month_tier", "v_team_month", "v_gap_analysis", "v_drill_stats",
    "v_participant_drill_stats", "v_candidate_overall", "v_candidate_state",
    "v_position_counts",
]


async def _count(session, relation: str) -> int:
    return (await session.execute(text(f"select count(*) from {relation}"))).scalar_one()


async def _count_foreign(session, relation: str, org_id) -> int:
    """Rows belonging to *another* org that this principal can see.

    Not `count(*) == 0`. Org B's manager legitimately sees Org B rows — they can
    read themselves and their own rep — so a bare count is the wrong assertion
    and it failed on `account` the first time this ran. The claim CMP-004 makes
    is narrower and sharper: **zero rows from the other tenant.**
    """
    return (
        await session.execute(
            text(f"select count(*) from {relation} where org_id = :org"), {"org": org_id}
        )
    ).scalar_one()


@pytest.mark.verifies("CMP-004", "AC-IDA-006")
@pytest.mark.parametrize("table", CUSTOMER_TABLES)
async def test_no_customer_table_leaks_across_orgs(as_principal, manager, world, table):
    """Org B's manager sees no Org A row, on every customer-data table.

    Parameterised over the whole table list rather than a representative sample:
    the one table nobody thought to check is exactly the one that leaks, and the
    generator makes it cheap to check all of them.
    """
    async with as_principal(manager(world.m3, world.org_b)) as session:
        assert await _count_foreign(session, table, world.org_a) == 0, (
            f"{table} returned Org A rows to an Org B principal — CMP-004 breach"
        )


@pytest.mark.verifies("CMP-004")
@pytest.mark.parametrize("view", DERIVED_VIEWS)
async def test_no_derived_view_leaks_across_orgs(as_principal, manager, world, view):
    """Same claim, through the aggregates.

    data FS-1 requires isolation proven **through the views**, not only the base
    tables, because the failure mode is a view definition rather than a policy.
    """
    async with as_principal(manager(world.m3, world.org_b)) as session:
        assert await _count_foreign(session, view, world.org_a) == 0, (
            f"{view} leaked across the org boundary — check its security_invoker reloption"
        )


@pytest.mark.verifies("FR-IDA-009", "AC-IDA-007")
async def test_manager_cannot_read_another_teams_drills(as_principal, manager, world):
    """M2 shares an org with M1 and must still see none of M1's drills."""
    async with as_principal(manager(world.m2, world.org_a)) as session:
        rows = (
            await session.execute(
                text("select id from drill where id = :d"), {"d": world.published_drill}
            )
        ).all()
    assert rows == [], "a manager read another team's drill inside the same org"


@pytest.mark.verifies("FR-IDA-009")
async def test_manager_cannot_read_another_teams_attempts(as_principal, manager, world):
    async with as_principal(manager(world.m2, world.org_a)) as session:
        assert await _count(session, "attempt") == 0


@pytest.mark.verifies("AC-IDA-006")
async def test_denial_is_indistinguishable_from_absence(as_principal, manager, world):
    """A cross-scope id and a nonexistent id return the identical empty result.

    At the database layer that is all "indistinguishable" can mean; the byte- and
    timing-equality of the HTTP response is the L7 battery's claim
    (`platform.errors.denial`). This asserts the layer underneath it: the query
    itself cannot tell the caller which case it was.
    """
    from uuid import uuid4

    async with as_principal(manager(world.m3, world.org_b)) as session:
        cross_scope = (
            await session.execute(text("select * from drill where id = :d"), {"d": world.published_drill})
        ).all()
        absent = (
            await session.execute(text("select * from drill where id = :d"), {"d": uuid4()})
        ).all()

    assert cross_scope == absent == []


@pytest.mark.verifies("CMP-004")
async def test_scopeless_transaction_reads_nothing(app_engine, world):
    """No scope context at all → zero rows, not every row.

    ADR-0031 decision 1: missing context reads as NULL and matches nothing, deny
    by default. This is the property that makes a forgotten `apply_scope` a dead
    request rather than a tenant-wide leak — worth asserting directly, because
    every other test in this file establishes its context correctly and so would
    never notice if the default inverted.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    maker = async_sessionmaker(app_engine, expire_on_commit=False)
    async with maker() as session, session.begin():
        for table in ("account", "drill", "attempt", "candidate"):
            assert await _count(session, table) == 0, (
                f"{table} returned rows with NO scope context set — the default is open, "
                "which inverts ADR-0031 decision 1"
            )
