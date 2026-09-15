"""Collect five real evaluator runs for each fixed Phase 4 pilot fixture.

The output is content-free study evidence: hashes, model identity, server-derived
scores, and validation state. Transcript and generated commentary stay in memory.
Fixture mode is deliberately rejected because it is integration scaffolding, not
model-validity evidence.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from bluelab.adapters.evaluator_llm import (
    EVALUATOR_MODEL,
    EVALUATOR_SENTINEL_MODEL,
    AnthropicEvaluatorProvider,
    EvaluationCall,
    EvaluatorProvider,
    GradingBasis,
)
from bluelab.modules.review.grading import (
    DimensionResult,
    EvaluationValidationError,
    ValidatedEvaluation,
    validate_evaluation,
    weighted_overall,
)
from bluelab.modules.review.validity import (
    AIRunCollection,
    AIRunEvidence,
    InjectionControlEvidence,
    PilotCorpus,
    PilotFixture,
    StudyEvidenceError,
    corpus_digest,
    load_pilot_corpus,
)
from bluelab.platform.config import get_settings
from bluelab.platform.queue.catalog import Lane, spec_for


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus", type=Path, default=Path("tests/fixtures/grading/corpus.v1.json")
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", choices=("primary", "sentinel"), default="primary")
    return parser.parse_args()


def _write_checkpoint(output: Path, collection: AIRunCollection) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(f"{output.suffix}.tmp")
    temporary.write_text(
        collection.model_dump_json(indent=2), encoding="utf-8", newline="\n"
    )
    temporary.replace(output)


def _load_checkpoint(
    output: Path, *, expected_corpus_sha256: str, expected_model: str
) -> tuple[str, list[AIRunEvidence]]:
    if not output.exists():
        return datetime.now(UTC).isoformat(), []
    try:
        collection = AIRunCollection.model_validate_json(
            output.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise StudyEvidenceError("grading study checkpoint is invalid") from exc
    if collection.corpus_sha256 != expected_corpus_sha256:
        raise StudyEvidenceError("grading study checkpoint uses another corpus")
    if any(run.model_version != expected_model for run in collection.runs):
        raise StudyEvidenceError("grading study checkpoint uses another model")
    identities = [(run.fixture_id, run.run_index) for run in collection.runs]
    if len(set(identities)) != len(identities):
        raise StudyEvidenceError("grading study checkpoint contains duplicate runs")
    return collection.collected_at, list(collection.runs)


def _derived_overall(
    fixture: PilotFixture, dimensions: dict[str, float]
) -> float:
    rubric = fixture.frozen_basis["rubric"]
    expected_ids = {str(item["dimension_id"]) for item in rubric}
    if set(dimensions) != expected_ids:
        raise StudyEvidenceError("grading study dimensions do not match the frozen rubric")
    results = [
        DimensionResult(
            rubric_dimension_id=UUID(str(item["dimension_id"])),
            weight=int(item["weight"]),
            score=Decimal(str(dimensions[str(item["dimension_id"])])),
            note=None,
        )
        for item in rubric
    ]
    return float(weighted_overall(results))


def _refresh_derived_overalls(
    corpus: PilotCorpus, runs: list[AIRunEvidence]
) -> list[AIRunEvidence]:
    """Recompute checkpoint aggregates from immutable dimension evidence."""
    fixtures = {fixture.fixture_id: fixture for fixture in corpus.fixtures}
    refreshed: list[AIRunEvidence] = []
    for run in runs:
        fixture = fixtures.get(run.fixture_id)
        if fixture is None:
            raise StudyEvidenceError("grading study checkpoint references another corpus")
        control = run.injection_control
        if control is not None:
            control = control.model_copy(
                update={"overall": _derived_overall(fixture, control.dimensions)}
            )
        refreshed.append(
            run.model_copy(
                update={
                    "overall": _derived_overall(fixture, run.dimensions),
                    "injection_control": control,
                }
            )
        )
    return refreshed


async def _evaluate_validated(
    provider: EvaluatorProvider, basis: GradingBasis
) -> tuple[EvaluationCall, ValidatedEvaluation]:
    """Mirror the grading lane's bounded retries around semantic validation."""
    last_error: EvaluationValidationError | None = None
    for _attempt in range(spec_for(Lane.GRADE_ATTEMPT).max_attempts):
        call = await provider.evaluate(basis)
        try:
            return call, validate_evaluation(basis, call.output)
        except EvaluationValidationError as exc:
            last_error = exc
    if last_error is None:  # pragma: no cover - the lane always has an attempt
        raise RuntimeError("grading lane has no configured attempts")
    raise last_error


async def _collect(
    corpus_path: Path, model_kind: str, output: Path
) -> AIRunCollection:
    corpus = load_pilot_corpus(corpus_path)
    settings = get_settings()
    if settings.vendor_fixture_mode:
        raise RuntimeError("VENDOR_FIXTURE_MODE must be false for validity evidence")
    model = EVALUATOR_MODEL if model_kind == "primary" else EVALUATOR_SENTINEL_MODEL
    provider = AnthropicEvaluatorProvider(settings, model=model)
    corpus_sha256 = corpus_digest(corpus)
    collected_at, runs = _load_checkpoint(
        output,
        expected_corpus_sha256=corpus_sha256,
        expected_model=model,
    )
    runs = _refresh_derived_overalls(corpus, runs)
    completed = {(run.fixture_id, run.run_index) for run in runs}
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
        for run_index in range(1, 6):
            if (fixture.fixture_id, run_index) in completed:
                continue
            call, validated = await _evaluate_validated(provider, basis)
            overall = weighted_overall(validated.dimensions)
            output_bytes = json.dumps(
                call.output.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            injection_control: InjectionControlEvidence | None = None
            if fixture.injection_case:
                if (
                    fixture.injection_control_transcript is None
                    or fixture.injection_control_transcript_hash is None
                ):
                    raise RuntimeError("validated injection fixture has no paired control")
                control_payload = basis.model_dump(mode="python")
                control_payload["transcript"] = fixture.injection_control_transcript
                control_basis = GradingBasis.model_validate(control_payload)
                control_call, control_validated = await _evaluate_validated(
                    provider, control_basis
                )
                control_output_bytes = json.dumps(
                    control_call.output.model_dump(mode="json"),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                injection_control = InjectionControlEvidence(
                    transcript_hash=fixture.injection_control_transcript_hash,
                    output_hash=hashlib.sha256(control_output_bytes).hexdigest(),
                    model_version=control_call.model_version,
                    aggregate_source="server_weighted_dimensions",
                    pipeline_validation="passed",
                    overall=float(weighted_overall(control_validated.dimensions)),
                    dimensions={
                        str(item.rubric_dimension_id): float(item.score)
                        for item in control_validated.dimensions
                    },
                )
            runs.append(
                AIRunEvidence(
                    fixture_id=fixture.fixture_id,
                    run_index=run_index,
                    model_version=call.model_version,
                    content_hash=fixture.content_hash,
                    transcript_hash=fixture.transcript_hash,
                    output_hash=hashlib.sha256(output_bytes).hexdigest(),
                    aggregate_source="server_weighted_dimensions",
                    pipeline_validation="passed",
                    overall=float(overall),
                    dimensions={
                        str(item.rubric_dimension_id): float(item.score)
                        for item in validated.dimensions
                    },
                    injection_control=injection_control,
                )
            )
            collection = AIRunCollection(
                schema_version=1,
                corpus_sha256=corpus_sha256,
                collected_at=collected_at,
                runs=runs,
            )
            _write_checkpoint(output, collection)
            completed.add((fixture.fixture_id, run_index))
            print(
                f"collected {fixture.fixture_id} run {run_index} "
                f"({len(runs)}/50)",
                flush=True,
            )
    return AIRunCollection(
        schema_version=1,
        corpus_sha256=corpus_sha256,
        collected_at=collected_at,
        runs=runs,
    )


def main() -> int:
    args = _arguments()
    collection = asyncio.run(_collect(args.corpus, args.model, args.output))
    _write_checkpoint(args.output, collection)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
