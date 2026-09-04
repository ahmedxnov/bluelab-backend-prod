"""T-5 — drill publish and freeze (data/02 §1).

Status transition `draft -> published`, the sum-to-100 gate (`409 weights-not-100`,
carrying `delta`), the generation-complete check, and the content freeze. Frozen
rows are guarded by triggers in `sql/triggers/`; erasure is the only sanctioned
writer that crosses them (ADR-0032, ADR-0033).

## Publish is the moment the answer key is copied, not referenced

The team's live facts are snapshotted **into** `drill.answer_key` (FR-DRL-014,
FR-KNW-007). A reference would mean a fact edited next month silently changes what
an attempt was graded against last month — and with it, a rep's recorded score and
a candidate's hiring decision. The copy is what makes a score mean the same thing
a year later (ADR-0007).

## Why sum-to-100 is here and not a constraint

A row constraint cannot see its siblings. The gate lives in this transaction, and
`meta.delta` carries the shortfall so the UI can say "+3 to balance" rather than
"invalid" (AC-DRL-003).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.platform.errors import catalog
from bluelab.platform.errors.denial import ProblemError

REQUIRED_WEIGHT_TOTAL = 100

_WEIGHT_TOTAL = text(
    "select coalesce(sum(weight), 0), count(*) from rubric_dimension where drill_id = :drill"
)

_LIVE_FACTS = text(
    """
    select coalesce(
        jsonb_agg(jsonb_build_object('label', f.label, 'value', f.value, 'note', f.note)
                  order by f.ord),
        '[]'::jsonb)
    from product_fact f
    join fact_set s on s.id = f.fact_set_id and s.kind = 'live'
    join product_document d on d.id = s.document_id
    where d.team_id = :team_id
    """
)

_PUBLISH = text(
    """
    update drill
       set status = 'published', published_at = now(), updated_at = now(),
           scenario = cast(:scenario as jsonb), answer_key = cast(:answer_key as jsonb),
           label = :label, content_hash = :content_hash
     where id = :drill and status = 'draft'
    """
)


async def publish_drill(
    session: AsyncSession,
    *,
    drill_id: UUID,
    team_id: UUID,
    scenario: dict[str, Any],
    label: str,
    content_hash: str,
) -> None:
    """Run T-5 inside the caller's transaction.

    Raises:
        ProblemError: `409 weights-not-100` carrying `delta`,
            `409 generation-incomplete` if the rubric was never generated, or
            `409 drill-not-draft` on a replay against an already-published drill.
    """
    import json

    total, dimension_count = (await session.execute(_WEIGHT_TOTAL, {"drill": drill_id})).one()

    if dimension_count == 0:
        # No rubric means generation never completed. Publishing would create a
        # drill that can be taken and cannot be graded (FR-DRL-006).
        raise ProblemError(catalog.GENERATION_INCOMPLETE)

    if total != REQUIRED_WEIGHT_TOTAL:
        # `delta` is the contract, not a nicety: the UI renders "{±delta} to
        # balance" from it (AC-DRL-003, ux/01 §5.5).
        raise ProblemError(
            catalog.WEIGHTS_NOT_100, meta={"delta": REQUIRED_WEIGHT_TOTAL - int(total)}
        )

    answer_key = (await session.execute(_LIVE_FACTS, {"team_id": team_id})).scalar_one()

    published = await session.execute(
        _PUBLISH,
        {
            "drill": drill_id,
            "scenario": json.dumps({"v": 1, **scenario}),
            # Snapshot format carries its version so every historical `v` stays
            # readable forever — readers never migrate, because frozen rows are
            # never rewritten (data/04 §5).
            "answer_key": json.dumps({"v": 1, "facts": answer_key}),
            "label": label,
            "content_hash": content_hash,
        },
    )
    if published.rowcount == 0:  # type: ignore[attr-defined]  # SQLAlchemy types async execute() as Result[Any]; the UPDATE it returns is a CursorResult at runtime
        # Conditional on `status = 'draft'`, so two racing publishes cannot both
        # win and a replay against a published drill is refused rather than
        # rewriting frozen content.
        raise ProblemError(catalog.DRILL_NOT_DRAFT)
