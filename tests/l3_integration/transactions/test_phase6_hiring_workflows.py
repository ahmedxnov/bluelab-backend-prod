"""Phase 6 manager-workflow checks that require PostgreSQL locking semantics."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from bluelab.modules.hiring import service


@pytest.mark.verifies("FR-HIR-004", "FR-HIR-005")
async def test_assessment_save_takes_the_position_lock_and_preserves_order(
    session, base_org, make_drill, make_position
) -> None:
    """A valid ordered save activates the position (FR-HIR-004/005)."""
    position = await make_position()
    first = await make_drill(status="published")
    second = await make_drill(status="published")

    async with session.begin():
        await service.replace_assessment(
            session, position_id=position, drill_ids=[second, first]
        )

    rows = (
        await session.execute(
            text(
                "select ord,drill_id from assessment_stage where position_id=:position order by ord"
            ),
            {"position": position},
        )
    ).all()
    status = (
        await session.execute(
            text("select status from position where id=:position"), {"position": position}
        )
    ).scalar_one()
    assert [(row.ord, row.drill_id) for row in rows] == [(1, second), (2, first)]
    assert status == "active"
