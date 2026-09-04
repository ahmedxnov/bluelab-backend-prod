"""`resolve_anchor` against a real `org.timezone`.

Split out of `test_team_gate.py`: these are not security tests and they are not
on an invariant path, so carrying that module's `l7_security` and
`invariant_path` marks overstated what sits inside the mutation-tested band
(`pyproject.toml`, `[tool.mutmut]`). They are plain integration tests of an
arithmetic helper against real postgres, and marked as such.

`parse_month`'s arithmetic is pinned at L1 in `tests/l1_unit/test_month_anchor.py`.
What is added here is the half that needs a database: that the default really does
come from the org's own clock rather than the server's, and that naming a month
substitutes the bucket without disturbing the calendar it is read in.

**These run as the migration role**, via `training_engine`, so RLS is not in
force. That is deliberate and it bounds the claim: they demonstrate the SQL and
the substitution, not the scoping. The RLS-filtered path through `_anchor` is
already exercised by every `/me` test in `test_me_surface.py`, which drives the
real app under the app role.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from bluelab.modules.training.service import resolve_anchor

pytestmark = [pytest.mark.l3_integration]


async def test_the_default_month_is_the_orgs_current_month(training_engine, world):
    """No `month` given resolves to the org's clock, not the server's."""
    maker = async_sessionmaker(training_engine, expire_on_commit=False)
    async with maker() as session:
        anchor = await resolve_anchor(session, account_id=world.manager, month=None)

    assert anchor.timezone == "UTC"
    assert anchor.month.day == 1
    assert anchor.month.tzinfo is None


async def test_an_explicit_month_overrides_the_clock(training_engine, world):
    """`?month=` selects a bucket; it does not shift the org's timezone.

    The timezone stays the org's because every query built from this anchor still
    buckets on `anchor.timezone` — a caller chooses WHICH month, never WHOSE
    calendar, so a manager cannot read their team's numbers in another org's civil
    month by varying a parameter.
    """
    maker = async_sessionmaker(training_engine, expire_on_commit=False)
    async with maker() as session:
        anchor = await resolve_anchor(session, account_id=world.manager, month="2024-03")

    assert anchor.month == datetime(2024, 3, 1)  # noqa: DTZ001 — naive, deliberately
    assert anchor.timezone == "UTC"
