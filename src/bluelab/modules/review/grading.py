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

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.adapters.evaluator_llm import EvaluationOutput, GradingBasis
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


@dataclass(frozen=True, slots=True)
class ValidatedEvaluation:
    dimensions: list[DimensionResult]
    moments: list[MomentResult]
    takeaway: str


class EvaluationValidationError(ValueError):
    """A structurally valid model response violated grading semantics."""


_ARABIC = re.compile(r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff]")


def _normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _require_english(label: str, value: str | None) -> None:
    if value is None or not value.strip() or _ARABIC.search(value):
        raise EvaluationValidationError(f"{label} must be non-empty English commentary")


def validate_evaluation(
    basis: GradingBasis,
    output: EvaluationOutput,
    *,
    concealed_fragments: tuple[str, ...] = (),
) -> ValidatedEvaluation:
    """Validate evidence and derive the only persistence-ready payload."""
    rubric_by_id = {item.dimension_id: item for item in basis.rubric}
    if len(rubric_by_id) != len(basis.rubric):
        raise EvaluationValidationError("rubric dimension ids must be unique")
    judgment_ids = [item.dimension_id for item in output.dimensions]
    if len(set(judgment_ids)) != len(judgment_ids) or set(judgment_ids) != set(
        rubric_by_id
    ):
        raise EvaluationValidationError(
            "evaluator dimension ids must match every frozen rubric dimension exactly once"
        )

    transcript_by_seq = {entry.seq: entry for entry in basis.transcript}
    if len(transcript_by_seq) != len(basis.transcript):
        raise EvaluationValidationError("transcript sequence anchors must be unique")
    if not any(entry.speaker == "participant" for entry in basis.transcript):
        raise EvaluationValidationError(
            "a completed attempt transcript must contain participant speech"
        )

    comments = [output.takeaway]
    _require_english("takeaway", output.takeaway)
    dimensions: list[DimensionResult] = []
    for judgment in output.dimensions:
        _require_english("dimension note", judgment.note)
        comments.append(judgment.note)
        if judgment.score != judgment.score.quantize(Decimal("0.1")):
            raise EvaluationValidationError("dimension scores must use 0.1 increments")
        if len(set(judgment.evidence_seq)) != len(judgment.evidence_seq):
            raise EvaluationValidationError("dimension evidence anchors must be unique")
        if any(seq not in transcript_by_seq for seq in judgment.evidence_seq):
            raise EvaluationValidationError("dimension evidence anchor is not in transcript")
        rubric = rubric_by_id[judgment.dimension_id]
        dimensions.append(
            DimensionResult(
                rubric_dimension_id=judgment.dimension_id,
                weight=rubric.weight,
                score=judgment.score,
                note=judgment.note,
            )
        )

    if not output.moments:
        raise EvaluationValidationError("at least one evidence-anchored moment is required")
    moments: list[MomentResult] = []
    for moment in output.moments:
        if moment.dimension_id not in rubric_by_id:
            raise EvaluationValidationError("moment dimension is not in frozen rubric")
        anchor = transcript_by_seq.get(moment.transcript_seq)
        if anchor is None:
            raise EvaluationValidationError("moment evidence anchor is not in transcript")
        if moment.severity in {"amber", "red"}:
            if anchor.speaker != "participant":
                raise EvaluationValidationError(
                    "amber or red moment must cite participant speech"
                )
            if not moment.quote or moment.quote not in anchor.text:
                raise EvaluationValidationError(
                    "amber or red quote must be verbatim participant speech"
                )
            _require_english("try_instead", moment.try_instead)
            _require_english("why_it_matters", moment.why_it_matters)
            comments.extend([moment.try_instead or "", moment.why_it_matters or ""])
        moments.append(
            MomentResult(
                at_ms=anchor.at_ms,
                severity=moment.severity,
                rubric_dimension_id=moment.dimension_id,
                transcript_seq=moment.transcript_seq,
                quote=moment.quote,
                try_instead=moment.try_instead,
                why_it_matters=moment.why_it_matters,
            )
        )

    normalized_comments = "\n".join(_normalized(item) for item in comments)
    for fragment in concealed_fragments:
        candidate = _normalized(fragment)
        if len(candidate) >= 4 and candidate in normalized_comments:
            raise EvaluationValidationError(
                "generated commentary contains concealed information"
            )

    return ValidatedEvaluation(
        dimensions=dimensions,
        moments=moments,
        takeaway=output.takeaway,
    )


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
    if not dimensions:
        raise ValueError("at least one rubric dimension is required")
    dimension_ids = [dimension.rubric_dimension_id for dimension in dimensions]
    if len(set(dimension_ids)) != len(dimension_ids):
        raise ValueError("rubric dimension ids must be unique")
    if any(
        dimension.weight < 0
        or dimension.score < Decimal(0)
        or dimension.score > Decimal(10)
        or dimension.score != dimension.score.quantize(Decimal("0.1"))
        for dimension in dimensions
    ):
        raise ValueError("dimension weights and scores are outside persistence bounds")
    total_weight = sum(d.weight for d in dimensions)
    if total_weight != 100:
        raise ValueError(
            "rubric weights must sum to 100 — the drill's freeze guard has been breached"
        )
    weighted = sum(Decimal(d.weight) * d.score for d in dimensions)
    return quantize_score(weighted / Decimal(total_weight))


def _validate_persistence_payload(
    *,
    dimensions: list[DimensionResult],
    moments: list[MomentResult],
    takeaway: str | None,
    grading_meta: dict[str, Any],
) -> None:
    _ = weighted_overall(dimensions)
    if takeaway is None or not takeaway.strip():
        raise ValueError("a new scorecard requires a coach takeaway")
    _require_english("takeaway", takeaway)
    if not moments:
        raise ValueError("a new scorecard requires at least one evidence-anchored moment")
    dimension_ids = {item.rubric_dimension_id for item in dimensions}
    for dimension in dimensions:
        _require_english("dimension note", dimension.note)
    for moment in moments:
        if (
            moment.rubric_dimension_id not in dimension_ids
            or moment.at_ms < 0
            or moment.severity not in {"green", "amber", "red"}
        ):
            raise ValueError("moment is outside the validated scorecard")
        if moment.severity in {"amber", "red"}:
            if not moment.quote:
                raise ValueError("amber and red moments require a verbatim quote")
            _require_english("try_instead", moment.try_instead)
            _require_english("why_it_matters", moment.why_it_matters)

    forbidden = ("prompt", "response", "transcript", "quote", "audio", "recording")
    for key, value in grading_meta.items():
        if any(token in key.casefold() for token in forbidden):
            raise ValueError("grading metadata keys must be content-free")
        if isinstance(value, str) and len(value) <= 200:
            continue
        if isinstance(value, (int, float, bool)) or value is None:
            continue
        raise ValueError("grading metadata values must be bounded content-free scalars")


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

    _validate_persistence_payload(
        dimensions=dimensions,
        moments=moments,
        takeaway=takeaway,
        grading_meta=grading_meta,
    )

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
