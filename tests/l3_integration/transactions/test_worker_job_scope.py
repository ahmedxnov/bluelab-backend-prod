"""A work-plane job establishes tenant scope before its first query."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from bluelab.platform.queue.catalog import Lane
from bluelab.platform.queue.compat import envelope
from bluelab.platform.queue.context import job_transaction

pytestmark = pytest.mark.l3_integration


@pytest.mark.verifies("ADR-0031")
async def test_job_context_sets_system_org_and_team_scope(base_org):
    body = envelope(
        {"email_send_id": str(base_org["manager"])},
        org_id=base_org["org"],
        team_id=base_org["manager"],
    )

    async with job_transaction(
        Lane.DISPATCH_EMAIL, body, job_id="scope-proof"
    ) as (session, decoded):
        gucs = (
            await session.execute(
                text(
                    "select current_setting('app.principal_kind', true),"
                    " current_setting('app.org_id', true),"
                    " current_setting('app.team_id', true)"
                )
            )
        ).one()

    assert decoded.org_id == base_org["org"]
    assert gucs == (
        "system",
        str(base_org["org"]),
        str(base_org["manager"]),
    )
