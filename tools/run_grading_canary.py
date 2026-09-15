"""Re-grade the fixed panel and emit the content-free grading-drift canary."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from bluelab.adapters.evaluator_llm import (
    EVALUATOR_MODEL,
    EVALUATOR_SENTINEL_MODEL,
    AnthropicEvaluatorProvider,
    GradingBasis,
)
from bluelab.modules.review.grading import validate_evaluation, weighted_overall
from bluelab.modules.review.validity import (
    AIRunEvidence,
    compare_canary,
    load_ai_run_collection,
    load_pilot_corpus,
)
from bluelab.platform.config import get_settings
from bluelab.platform.telemetry import metrics


async def _collect_live_candidate(corpus_path: Path, model_kind: str) -> list[AIRunEvidence]:
    corpus = load_pilot_corpus(corpus_path)
    settings = get_settings()
    if settings.vendor_fixture_mode:
        raise RuntimeError("VENDOR_FIXTURE_MODE must be false for a live canary")
    model = EVALUATOR_MODEL if model_kind == "primary" else EVALUATOR_SENTINEL_MODEL
    provider = AnthropicEvaluatorProvider(settings, model=model)
    runs: list[AIRunEvidence] = []
    for fixture in corpus.fixtures:
        basis = GradingBasis.model_validate(
            {
                "attempt_id": fixture.attempt_id,
                "content_hash": fixture.content_hash,
                "call_type": fixture.call_type,
                "scenario": fixture.frozen_basis["scenario"],
                "answer_key": fixture.frozen_basis["answer_key"],
                "rubric": fixture.frozen_basis["rubric"],
                "transcript": fixture.transcript,
            }
        )
        call = await provider.evaluate(basis)
        validated = validate_evaluation(basis, call.output)
        output_bytes = json.dumps(
            call.output.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        runs.append(
            AIRunEvidence(
                fixture_id=fixture.fixture_id,
                run_index=1,
                model_version=call.model_version,
                content_hash=fixture.content_hash,
                transcript_hash=fixture.transcript_hash,
                output_hash=hashlib.sha256(output_bytes).hexdigest(),
                aggregate_source="server_weighted_dimensions",
                pipeline_validation="passed",
                overall=float(weighted_overall(validated.dimensions)),
                dimensions={
                    str(item.rubric_dimension_id): float(item.score)
                    for item in validated.dimensions
                },
            )
        )
        metrics.record_model_version(
            capability="evaluator_canary", model_version=call.model_version
        )
        metrics.record_grading_cost(cost_usd=call.cost_usd)
    return runs


async def _run() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus", type=Path, default=Path("tests/fixtures/grading/corpus.v1.json")
    )
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument(
        "--candidate",
        type=Path,
        help="Pre-collected five-run candidate panel; omit to execute a live re-grade.",
    )
    parser.add_argument("--model", choices=("primary", "sentinel"), default="primary")
    args = parser.parse_args()

    corpus = load_pilot_corpus(args.corpus)
    reference = load_ai_run_collection(args.reference, corpus)
    candidate_runs = (
        load_ai_run_collection(args.candidate, corpus).runs
        if args.candidate is not None
        else await _collect_live_candidate(args.corpus, args.model)
    )
    report = compare_canary(corpus, reference.runs, candidate_runs)
    for item in report.fixtures:
        metrics.record_grading_consistency(
            delta=item.overall_delta,
            score_level="overall",
            call_type=item.call_type,
        )
        metrics.record_grading_consistency(
            delta=item.maximum_dimension_delta,
            score_level="dimension",
            call_type=item.call_type,
        )
    print(
        json.dumps(
            {
                "observed_at": datetime.now(UTC).isoformat(),
                **asdict(report),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 1 if report.breached else 0


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
