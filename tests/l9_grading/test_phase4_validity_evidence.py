"""Phase 4 reproducible grading-validity evidence and pilot-corpus gates."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from tools.analyze_grading_study import phase4_pilot_outcome
from tools.collect_grading_study import (
    _load_checkpoint,
    _refresh_derived_overalls,
    _write_checkpoint,
)

from bluelab.modules.review.validity import (
    AIRunCollection,
    AIRunEvidence,
    ExpertStudyEvidence,
    StudyEvidenceError,
    analyze_consistency,
    analyze_expert_agreement,
    bland_altman,
    compare_canary,
    corpus_digest,
    icc_2_1,
    load_pilot_corpus,
    validate_blind_expert_evidence,
    wilson_interval,
)

pytestmark = [pytest.mark.l9_grading]


@pytest.mark.verifies("NFR-004", "SEC-029")
def test_authorized_pilot_corpus_is_fixed_hashed_and_covers_all_call_types() -> None:
    root = Path(__file__).resolve().parents[2]
    corpus = load_pilot_corpus(root / "tests" / "fixtures" / "grading" / "corpus.v1.json")

    assert len(corpus.fixtures) == 10
    assert {item.call_type for item in corpus.fixtures} == {
        "discovery",
        "post_proposal",
        "renewal",
        "upsell",
    }
    assert all(item.synthetic and item.authorized_for_grading_research for item in corpus.fixtures)
    assert all(item.attempt_status == "completed" for item in corpus.fixtures)
    assert sum(item.injection_case for item in corpus.fixtures) >= 2
    assert all(
        item.injection_control_transcript is not None
        and item.injection_control_transcript_hash is not None
        for item in corpus.fixtures
        if item.injection_case
    )
    assert corpus.verify_hashes() == []


@pytest.mark.verifies("NFR-004")
def test_wilson_interval_and_agreement_statistics_are_reproducible() -> None:
    low, high = wilson_interval(successes=20, trials=20)
    assert low == pytest.approx(0.8808421620)
    assert high == pytest.approx(1.0)

    ratings = [
        [7.0, 7.2, 6.9],
        [4.0, 4.1, 3.8],
        [8.0, 7.8, 8.1],
        [5.0, 5.2, 4.9],
    ]
    assert icc_2_1(ratings) == pytest.approx(0.9930354, rel=1e-5)
    agreement = bland_altman([7.0, 4.0, 8.0, 5.0], [7.1, 3.9, 7.9, 5.2])
    assert agreement.bias == pytest.approx(-0.025)
    assert agreement.lower_limit < agreement.bias < agreement.upper_limit


@pytest.mark.verifies("NFR-004", "CMP-003")
def test_blind_expert_gate_rejects_missing_nonblind_or_fabricated_evidence() -> None:
    with pytest.raises(StudyEvidenceError, match="three"):
        validate_blind_expert_evidence(
            {
                "raters": [{"rater_id": "expert-1", "blind": True}],
                "ratings": [],
            }
        )


def _perfect_runs(corpus):
    runs = []
    for fixture_number, fixture in enumerate(corpus.fixtures):
        score = 5.0 + (fixture_number % 5)
        dimensions = {
            item["dimension_id"]: score for item in fixture.frozen_basis["rubric"]
        }
        for run_index in range(1, 6):
            runs.append(
                AIRunEvidence(
                    fixture_id=fixture.fixture_id,
                    run_index=run_index,
                    model_version="claude-sonnet-5",
                    content_hash=fixture.content_hash,
                    transcript_hash=fixture.transcript_hash,
                    output_hash=f"{run_index:064x}",
                    aggregate_source="server_weighted_dimensions",
                    pipeline_validation="passed",
                    overall=score,
                    dimensions=dimensions,
                    injection_control=(
                        {
                            "transcript_hash": fixture.injection_control_transcript_hash,
                            "output_hash": f"{run_index + 100:064x}",
                            "model_version": "claude-sonnet-5",
                            "aggregate_source": "server_weighted_dimensions",
                            "pipeline_validation": "passed",
                            "overall": score,
                            "dimensions": dimensions,
                        }
                        if fixture.injection_case
                        else None
                    ),
                )
            )
    return runs


@pytest.mark.verifies("NFR-004")
def test_live_collection_checkpoint_is_atomic_and_corpus_bound(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    corpus = load_pilot_corpus(root / "tests" / "fixtures" / "grading" / "corpus.v1.json")
    collection = AIRunCollection(
        schema_version=1,
        corpus_sha256=corpus_digest(corpus),
        collected_at="2026-09-15T00:00:00+00:00",
        runs=_perfect_runs(corpus)[:1],
    )
    checkpoint = tmp_path / "primary-runs.json"

    _write_checkpoint(checkpoint, collection)
    collected_at, runs = _load_checkpoint(
        checkpoint,
        expected_corpus_sha256=corpus_digest(corpus),
        expected_model="claude-sonnet-5",
    )

    assert collected_at == collection.collected_at
    assert runs == collection.runs
    assert not checkpoint.with_suffix(".json.tmp").exists()
    with pytest.raises(StudyEvidenceError, match="another corpus"):
        _load_checkpoint(
            checkpoint,
            expected_corpus_sha256="f" * 64,
            expected_model="claude-sonnet-5",
        )


@pytest.mark.verifies("FR-SCR-004", "NFR-004")
def test_live_collection_refreshes_only_server_derived_overalls() -> None:
    root = Path(__file__).resolve().parents[2]
    corpus = load_pilot_corpus(root / "tests" / "fixtures" / "grading" / "corpus.v1.json")
    run = _perfect_runs(corpus)[0]
    stale = run.model_copy(update={"overall": 0.0})

    refreshed = _refresh_derived_overalls(corpus, [stale])

    assert refreshed[0].overall == run.overall
    assert refreshed[0].dimensions == run.dimensions
    assert refreshed[0].output_hash == run.output_hash


@pytest.mark.verifies("NFR-004", "SEC-029", "SEC-030")
def test_consistency_gate_requires_real_complete_pipeline_runs_and_reports_pilot_power() -> None:
    root = Path(__file__).resolve().parents[2]
    corpus = load_pilot_corpus(root / "tests" / "fixtures" / "grading" / "corpus.v1.json")
    runs = _perfect_runs(corpus)

    report = analyze_consistency(corpus, runs)

    assert report.complete is True
    assert report.all_observed_comparisons_within_tolerance is True
    assert report.all_pairwise_comparisons_within_tolerance is True
    assert report.injection_resistant is True
    # Ten pilot fixtures establish a reproducible signal, but no call-type stratum
    # can clear the prescribed Wilson lower-bound gate even at 100% observed.
    assert report.gate_status == "insufficient_sample"
    assert all(item.wilson_lower_95 < 0.95 for item in report.by_call_type)
    assert {item.fixture_id for item in report.injection_checks} == {"SYN-004", "SYN-009"}
    assert all(
        item.all_injection_control_comparisons_within_tolerance
        for item in report.injection_checks
    )

    manipulated = [
        item.model_copy(
            update={
                "overall": item.overall + 1.0,
                "dimensions": {
                    key: value + 1.0 for key, value in item.dimensions.items()
                },
            }
        )
        if item.fixture_id == "SYN-004"
        else item
        for item in runs
    ]
    manipulated_report = analyze_consistency(corpus, manipulated)
    assert manipulated_report.injection_resistant is False
    assert manipulated_report.gate_status == "fail"

    with pytest.raises(StudyEvidenceError, match="fixture evaluator"):
        analyze_consistency(
            corpus,
            [runs[0].model_copy(update={"model_version": "fixture-evaluator-v1-not-validity-evidence"}), *runs[1:]],
        )
    with pytest.raises(StudyEvidenceError, match="prescribed evaluator"):
        analyze_consistency(
            corpus,
            [
                runs[0].model_copy(update={"model_version": "unregistered-model"}),
                *runs[1:],
            ],
        )
    control = runs[15].injection_control
    assert control is not None
    with pytest.raises(StudyEvidenceError, match="paired model version"):
        analyze_consistency(
            corpus,
            [
                *runs[:15],
                runs[15].model_copy(
                    update={
                        "injection_control": control.model_copy(
                            update={"model_version": "claude-haiku-4-5-20251001"}
                        )
                    }
                ),
                *runs[16:],
            ],
        )
    with pytest.raises(StudyEvidenceError, match="exactly five"):
        analyze_consistency(corpus, runs[:-1])

    candidate = [
        item.model_copy(
            update={
                "overall": item.overall + (0.6 if item.fixture_id == "SYN-001" else 0.0),
                "dimensions": {
                    key: value + (0.6 if item.fixture_id == "SYN-001" else 0.0)
                    for key, value in item.dimensions.items()
                },
            }
        )
        for item in runs
    ]
    canary = compare_canary(corpus, runs, candidate)
    assert canary.breached is True
    assert any(item.overall_delta > 0.5 for item in canary.fixtures)

    single_run_candidate = [item for item in candidate if item.run_index == 1]
    assert compare_canary(corpus, runs, single_run_candidate).breached is True


@pytest.mark.verifies("NFR-004", "CMP-003", "SEC-031")
def test_expert_agreement_gate_is_complete_blind_hashed_and_bonferroni_adjusted() -> None:
    root = Path(__file__).resolve().parents[2]
    corpus = load_pilot_corpus(root / "tests" / "fixtures" / "grading" / "corpus.v1.json")
    runs = _perfect_runs(corpus)
    ratings = []
    for fixture_number, fixture in enumerate(corpus.fixtures):
        score = 5.0 + (fixture_number % 5)
        dimension_ids = [item["dimension_id"] for item in fixture.frozen_basis["rubric"]]
        for rater_number, delta in ((1, -0.1), (2, 0.0), (3, 0.1)):
            ratings.append(
                {
                    "fixture_id": fixture.fixture_id,
                    "rater_id": f"expert-{rater_number}",
                    "overall": score + delta,
                    "dimensions": {dimension_id: score + delta for dimension_id in dimension_ids},
                }
            )
    evidence = ExpertStudyEvidence.model_validate(
        {
            "schema_version": 1,
            "corpus_sha256": corpus_digest(corpus),
            "preregistration": {
                "registered_before_collection": True,
                "bias_threshold": 0.5,
                "icc_ceiling_margin": 0.15,
                "subgroup_bias_threshold": 0.5,
            },
            "evidence_attestation": "human_expert_ratings_collected",
            "randomized_call_order": True,
            "independent_scoring": True,
            "raters": [
                {
                    "rater_id": f"expert-{number}",
                    "blind": True,
                    "egyptian_market_sales_manager": True,
                    "credential_attested": True,
                }
                for number in range(1, 4)
            ],
            "ratings": ratings,
        }
    )

    report = analyze_expert_agreement(corpus, runs, evidence, bootstrap_samples=200)
    consistency = analyze_consistency(corpus, runs)

    assert report.complete is True
    assert report.gate_status == "pilot_signal"
    assert report.sample_size == 10
    assert report.human_human_ceiling > 0.99
    assert report.human_human_ceiling_ci_lower <= report.human_human_ceiling
    assert report.human_human_ceiling_ci_upper >= report.human_human_ceiling
    assert report.comparisons
    assert report.bonferroni_confidence > 0.95
    assert all(item.confidence == report.bonferroni_confidence for item in report.comparisons)
    assert all(-1 <= item.human_human_icc <= 1 for item in report.comparisons)
    assert all(
        item.human_human_icc_ci_lower <= item.human_human_icc_ci_upper
        for item in report.comparisons
    )
    assert phase4_pilot_outcome(consistency, report) == "pilot_signal"
    assert (
        phase4_pilot_outcome(replace(consistency, gate_status="fail"), report)
        == "escalate"
    )

    bad = evidence.model_copy(update={"corpus_sha256": "0" * 64})
    with pytest.raises(StudyEvidenceError, match="corpus hash"):
        analyze_expert_agreement(corpus, runs, bad, bootstrap_samples=100)

    with pytest.raises(StudyEvidenceError, match="blind"):
        validate_blind_expert_evidence(
            {
                "raters": [
                    {"rater_id": "expert-1", "blind": True},
                    {"rater_id": "expert-2", "blind": True},
                    {"rater_id": "expert-3", "blind": False},
                ],
                "ratings": [],
            }
        )

    with pytest.raises(StudyEvidenceError, match="attest"):
        validate_blind_expert_evidence(
            {
                "raters": [
                    {"rater_id": f"expert-{number}", "blind": True}
                    for number in range(1, 4)
                ],
                "ratings": [],
                "evidence_attestation": "synthetically generated",
            }
        )
