"""The attempt lifecycle — this module's published interface to `bluelab.calls`.

Admission (T-1) inserts the attempt, completion (T-2) inserts the transcript and
flips the status, interruption (T-6) voids it and reverses the allowance. The
call code calls in here; it does not touch `models.py`, because that would put
the concealment-bearing tables behind two doors instead of one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.adapters.evaluator_llm import (
    GradingBasis,
    RubricDimensionInput,
    TranscriptSignal,
)
from bluelab.platform.ids import new_id


@dataclass(frozen=True, slots=True)
class GradingWorkItem:
    basis: GradingBasis
    org_id: UUID
    team_id: UUID
    ended_at: datetime
    concealed_fragments: tuple[str, ...]


_GRADE_ATTEMPT = text(
    """
    select a.id,a.org_id,a.team_id,a.ended_at,a.status,
           d.call_type,d.scenario,d.answer_key,d.content_hash,
           exists(select 1 from scorecard s where s.attempt_id=a.id) as has_scorecard
    from attempt a
    join drill d on d.id=a.drill_id
    where a.id=:attempt
    """
)

_GRADE_RUBRIC = text(
    """
    select id,name,weight,rationale
    from rubric_dimension
    where drill_id=(select drill_id from attempt where id=:attempt)
    order by ord,id
    """
)

_GRADE_TRANSCRIPT = text(
    """
    select seq,speaker,at_ms,text
    from transcript_entry
    where attempt_id=:attempt
    order by seq
    """
)

_CONCEALED = text(
    """
    select challenges,hidden_motives
    from drill_concealed
    where drill_id=(select drill_id from attempt where id=:attempt)
    """
)


def _text_fragments(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [item for nested in value.values() for item in _text_fragments(nested)]
    if isinstance(value, list):
        return [item for nested in value for item in _text_fragments(nested)]
    return []


async def grading_work_item(
    session: AsyncSession, *, attempt_id: UUID
) -> GradingWorkItem | None:
    row = (await session.execute(_GRADE_ATTEMPT, {"attempt": attempt_id})).first()
    if (
        row is None
        or row.has_scorecard
        or row.status not in {"completed", "grading_pending"}
    ):
        return None
    if (
        row.ended_at is None
        or row.scenario is None
        or row.answer_key is None
        or row.content_hash is None
    ):
        raise ValueError("attempt is missing its frozen grading basis")
    rubric_rows = (
        await session.execute(_GRADE_RUBRIC, {"attempt": attempt_id})
    ).all()
    transcript_rows = (
        await session.execute(_GRADE_TRANSCRIPT, {"attempt": attempt_id})
    ).all()
    if not rubric_rows or not transcript_rows:
        raise ValueError("attempt has no rubric or transcript")
    if sum(int(item.weight) for item in rubric_rows) != 100:
        raise ValueError("frozen rubric weights do not sum to 100")
    concealed = (
        await session.execute(_CONCEALED, {"attempt": attempt_id})
    ).first()
    concealed_values: list[str] = []
    if concealed is not None:
        concealed_values.extend(_text_fragments(concealed.challenges))
        concealed_values.extend(_text_fragments(concealed.hidden_motives))
    basis = GradingBasis(
        attempt_id=attempt_id,
        content_hash=str(row.content_hash),
        call_type=row.call_type,
        scenario=dict(row.scenario),
        answer_key=dict(row.answer_key),
        rubric=[
            RubricDimensionInput(
                dimension_id=item.id,
                name=item.name,
                weight=int(item.weight),
                rationale=item.rationale,
            )
            for item in rubric_rows
        ],
        # Demeanor is deliberately omitted. SEC-031 keeps the signal disabled
        # until its separate fairness evidence gate passes.
        transcript=[
            TranscriptSignal(
                seq=int(item.seq),
                speaker=item.speaker,
                at_ms=int(item.at_ms),
                text=item.text,
            )
            for item in transcript_rows
        ],
    )
    return GradingWorkItem(
        basis=basis,
        org_id=row.org_id,
        team_id=row.team_id,
        ended_at=row.ended_at,
        concealed_fragments=tuple(concealed_values),
    )


_PENDING = text(
    """
    update attempt
       set status='grading_pending'
     where id=:attempt and status in ('completed','grading_pending')
    """
)

_OPEN_GRADING_FAULT = text(
    """
    insert into ops_fault (id,org_id,kind,attempt_id,detail)
    select :id,a.org_id,'grading_failure',a.id,cast(:detail as jsonb)
    from attempt a
    where a.id=:attempt
      and not exists (
          select 1 from ops_fault f
          where f.attempt_id=a.id and f.kind='grading_failure' and f.status='open'
      )
    """
)


async def mark_grading_exhausted(
    session: AsyncSession, *, attempt_id: UUID, retry_count: int
) -> None:
    # Serializes an ambiguous replay of the exhaustion action without requiring
    # a schema-level uniqueness rule absent from the authoritative model.
    await session.execute(
        text("select pg_advisory_xact_lock(hashtextextended(cast(:attempt as text),0))"),
        {"attempt": str(attempt_id)},
    )
    await session.execute(_PENDING, {"attempt": attempt_id})
    await session.execute(
        _OPEN_GRADING_FAULT,
        {
            "id": new_id(),
            "attempt": attempt_id,
            "detail": json.dumps(
                {
                    "error_class": "grading_retry_exhausted",
                    "retry_count": retry_count,
                },
                separators=(",", ":"),
            ),
        },
    )
