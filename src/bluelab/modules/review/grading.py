"""Grade-once semantics (T-3) and the review artifact: overall out of 10 with its
band, the coach takeaway, the pinned playback moment, tagged moments, and the
per-dimension rubric breakdown.

The band is computed server-side by the one banding function
(`sql/functions/fn_score_band.sql`) so banding is identical everywhere — clients
render, never re-derive (FR-SCR-005). Commentary is English; quoted speech is
verbatim Arabic (FR-SCR-008).

## Grade exactly once, and the constraint is the invariant

`scorecard.attempt_id` is unique, and T-3 inserts `ON CONFLICT DO NOTHING`. That
unique index — not a check in the worker, not a queue guarantee — is what makes
FR-SCR-003 true. The queue is at-least-once by design (ADR-0023), so a second
grader *will* eventually run; it must lose silently rather than produce a second
opinion on the same call.

The children only follow when the parent insert won. Writing dimension scores
against a scorecard another transaction created would attach this grader's
numbers to that grader's overall.

## The overall is arithmetic, never the model's

`overall_score` is the weight-weighted mean of the dimension scores, computed
here (FR-SCR-004). A model asked for its own average will occasionally return one
that does not match the numbers it just produced — and NFR-004's ±0.5 tolerance
is meaningless if the aggregate is itself a sample (ADR-0018).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.platform.db.types import quantize_score
from bluelab.platform.ids import new_id


@dataclass(frozen=True, slots=True)
class DimensionResult:
    rubric_dimension_id: UUID
    weight: int
    score: Decimal
    note: str | None


@dataclass(frozen=True, slots=True)
class MomentResult:
    at_ms: int
    severity: str
    rubric_dimension_id: UUID
    transcript_seq: int | None = None
    quote: str | None = None
    try_instead: str | None = None
    why_it_matters: str | None = None


_INSERT_SCORECARD = text(
    """
    insert into scorecard (id, org_id, team_id, attempt_id, overall_score, takeaway, grading_meta)
    values (:id, :org_id, :team_id, :attempt, :overall, :takeaway, cast(:meta as jsonb))
    on conflict (attempt_id) do nothing
    """
)

_INSERT_DIMENSION = text(
    """
    insert into dimension_score (scorecard_id, rubric_dimension_id, org_id, team_id, score, note)
    values (:scorecard, :dimension, :org_id, :team_id, :score, :note)
    """
)

_INSERT_MOMENT = text(
    """
    insert into moment (id, org_id, team_id, scorecard_id, attempt_id, transcript_seq,
                        at_ms, severity, rubric_dimension_id, quote, try_instead, why_it_matters)
    values (:id, :org_id, :team_id, :scorecard, :attempt, :seq,
            :at_ms, :severity, :dimension, :quote, :try_instead, :why)
    """
)

_MARK_GRADED = text(
    """
    update attempt set status = 'graded'
     where id = :attempt and status in ('completed','grading_pending')
    """
)


def weighted_overall(dimensions: list[DimensionResult]) -> Decimal:
    """The weight-weighted mean, to one decimal (FR-SCR-004).

    Raises:
        ValueError: If the weights sum to zero — a published drill cannot reach
            this state (T-5's sum-to-100 gate), so it means the rubric changed
            underneath a graded attempt, which the freeze guards forbid.
    """
    total_weight = sum(d.weight for d in dimensions)
    if total_weight <= 0:
        raise ValueError("rubric weights sum to zero — the drill's freeze guard has been breached")
    weighted = sum(Decimal(d.weight) * d.score for d in dimensions)
    return quantize_score(weighted / Decimal(total_weight))


async def write_scorecard(
    session: AsyncSession,
    *,
    attempt_id: UUID,
    org_id: UUID,
    team_id: UUID,
    dimensions: list[DimensionResult],
    moments: list[MomentResult],
    takeaway: str | None,
    grading_meta: dict[str, Any],
) -> UUID | None:
    """Run T-3 inside the caller's transaction.

    Returns:
        The new scorecard id, or **None** if another grader already wrote one.
        None is a success: the attempt is graded, just not by this call.

    Args:
        grading_meta: Model ids, versions, content hash — **content-free**
            (R-16, NFR-004 evidence). Never the prompt, never the response.
    """
    import json

    scorecard_id = new_id()
    inserted = await session.execute(
        _INSERT_SCORECARD,
        {
            "id": scorecard_id,
            "org_id": org_id,
            "team_id": team_id,
            "attempt": attempt_id,
            "overall": weighted_overall(dimensions),
            "takeaway": takeaway,
            "meta": json.dumps(grading_meta),
        },
    )
    if inserted.rowcount == 0:  # type: ignore[attr-defined]  # SQLAlchemy types async execute() as Result[Any]; the UPDATE it returns is a CursorResult at runtime
        # The second grader loses, silently (FR-SCR-003). Not an error: the queue
        # is at-least-once, so this is the designed outcome of a retry after an
        # ambiguous first attempt.
        return None

    for dimension in dimensions:
        await session.execute(
            _INSERT_DIMENSION,
            {
                "scorecard": scorecard_id,
                "dimension": dimension.rubric_dimension_id,
                "org_id": org_id,
                "team_id": team_id,
                "score": dimension.score,
                "note": dimension.note,
            },
        )

    for moment in moments:
        await session.execute(
            _INSERT_MOMENT,
            {
                "id": new_id(),
                "org_id": org_id,
                "team_id": team_id,
                "scorecard": scorecard_id,
                "attempt": attempt_id,
                "seq": moment.transcript_seq,
                "at_ms": moment.at_ms,
                "severity": moment.severity,
                "dimension": moment.rubric_dimension_id,
                "quote": moment.quote,
                "try_instead": moment.try_instead,
                "why": moment.why_it_matters,
            },
        )

    await session.execute(_MARK_GRADED, {"attempt": attempt_id})
    return scorecard_id
