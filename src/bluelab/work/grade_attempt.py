"""Grading (FR-SCR-001…009) under NFR-005: p50 <= 20 s, p95 <= 45 s.

Idempotency is `scorecard.attempt_id` unique — T-3 inserts `ON CONFLICT DO
NOTHING`, so a second grade is a silent no-op. While failing, the attempt reads
as `grading_pending` and the participant sees *preparing*, never an error; on
retry exhaustion it raises an `ops_fault`, and it **never voids the call**
(FR-SCR-009).

Uses the decomposed per-dimension evaluator pass of ADR-0018; consistency is
measured against NFR-004 (+/- 0.5 overall, +/- 1 per dimension) by the L9 variance
study and watched in operation by the re-grade canary.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.adapters.evaluator_llm import (
    EvaluatorProvider,
    create_evaluator_provider,
)
from bluelab.modules.review import attempts
from bluelab.modules.review.grading import validate_evaluation, write_scorecard
from bluelab.platform.config import Settings
from bluelab.platform.queue.catalog import Lane, spec_for
from bluelab.platform.queue.compat import JobEnvelope
from bluelab.platform.queue.runtime import JobRegistration
from bluelab.platform.telemetry import metrics


def registration(
    settings: Settings, *, provider: EvaluatorProvider | None = None
) -> JobRegistration:
    resolved = provider or create_evaluator_provider(settings)

    async def handle(session: AsyncSession, job: JobEnvelope) -> None:
        attempt_id = UUID(str(job.args["attempt_id"]))
        item = await attempts.grading_work_item(session, attempt_id=attempt_id)
        if item is None:
            return
        call = await resolved.evaluate(item.basis)
        # Spend and model identity belong to the provider call, regardless of
        # whether schema/semantic validation later rejects the output or this
        # worker loses the grade-once persistence race.
        metrics.record_grading_cost(cost_usd=call.cost_usd)
        metrics.record_model_version(
            capability="evaluator", model_version=call.model_version
        )
        validated = validate_evaluation(
            item.basis,
            call.output,
            concealed_fragments=item.concealed_fragments,
        )
        scorecard_id = await write_scorecard(
            session,
            attempt_id=attempt_id,
            org_id=item.org_id,
            team_id=item.team_id,
            dimensions=validated.dimensions,
            moments=validated.moments,
            takeaway=validated.takeaway,
            grading_meta={
                "schema_version": 1,
                "provider": "anthropic" if not settings.vendor_fixture_mode else "fixture",
                "model_version": call.model_version,
                "content_hash": item.basis.content_hash,
                "thinking": "disabled",
                "sampling_parameters": "default",
                "input_tokens": call.input_tokens,
                "output_tokens": call.output_tokens,
                "evaluator_cost_usd": str(call.cost_usd),
            },
        )
        if scorecard_id is not None:
            metrics.record_grading_turnaround(
                duration_ms=max(
                    0.0,
                    (datetime.now(UTC) - item.ended_at.astimezone(UTC)).total_seconds()
                    * 1_000,
                )
            )

    async def exhausted(session: AsyncSession, job: JobEnvelope) -> None:
        await attempts.mark_grading_exhausted(
            session,
            attempt_id=UUID(str(job.args["attempt_id"])),
            retry_count=spec_for(Lane.GRADE_ATTEMPT).max_attempts,
        )

    return JobRegistration(
        lane=Lane.GRADE_ATTEMPT,
        handler=handle,
        on_exhausted=exhausted,
        exhaustion_outcome=metrics.JobOutcome.OPS_FAULT,
    )
