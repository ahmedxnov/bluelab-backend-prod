"""Dependency-free grading-consistency and expert-agreement evidence primitives."""

from __future__ import annotations

import hashlib
import json
import math
import random
from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from statistics import fmean, stdev
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

_PRESCRIBED_EVALUATOR_MODELS = frozenset(
    {"claude-sonnet-5", "claude-haiku-4-5-20251001"}
)


class StudyEvidenceError(ValueError):
    """Study input is incomplete, non-blind, inconsistent, or ineligible."""


class PilotFixture(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fixture_id: str = Field(pattern=r"^SYN-[0-9]{3}$")
    attempt_id: UUID
    synthetic: Literal[True]
    authorized_for_grading_research: Literal[True]
    authorization: str = Field(min_length=20)
    provenance: dict[str, Any]
    attempt_status: Literal["completed"]
    call_type: Literal["discovery", "post_proposal", "renewal", "upsell"]
    speech_register: str = Field(min_length=1, alias="register")
    coverage_tags: list[str] = Field(min_length=2)
    injection_case: bool = Field(default=False, alias="injection_probe")
    injection_control_transcript_hash: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    injection_control_transcript: list[dict[str, Any]] | None = None
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    transcript_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    frozen_basis: dict[str, Any]
    transcript: list[dict[str, Any]] = Field(min_length=2)

    def computed_content_hash(self) -> str:
        return _sha256(self.frozen_basis)

    def computed_transcript_hash(self) -> str:
        return _sha256(self.transcript)

    def computed_injection_control_transcript_hash(self) -> str | None:
        if self.injection_control_transcript is None:
            return None
        return _sha256(self.injection_control_transcript)

    @model_validator(mode="after")
    def _paired_injection_control(self) -> PilotFixture:
        control = self.injection_control_transcript
        control_hash = self.injection_control_transcript_hash
        if not self.injection_case:
            if control is not None or control_hash is not None:
                raise ValueError("only injection probes may define a paired control")
            return self
        if control is None or control_hash is None:
            raise ValueError("every injection probe requires a hashed paired control")
        if len(control) != len(self.transcript):
            raise ValueError("an injection control must preserve transcript structure")
        buyer_text_changed = False
        for probe_entry, control_entry in zip(self.transcript, control, strict=True):
            probe_structure = {
                key: value for key, value in probe_entry.items() if key != "text"
            }
            control_structure = {
                key: value for key, value in control_entry.items() if key != "text"
            }
            if probe_structure != control_structure:
                raise ValueError("an injection control may change buyer text only")
            if probe_entry.get("speaker") == "participant":
                if probe_entry != control_entry:
                    raise ValueError("an injection control must preserve participant evidence")
            elif probe_entry.get("speaker") == "buyer":
                buyer_text_changed |= probe_entry.get("text") != control_entry.get("text")
            else:
                raise ValueError("an injection control has an invalid speaker")
        if not buyer_text_changed:
            raise ValueError("an injection control must remove an injected buyer instruction")
        return self


class PilotCorpus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    corpus_id: Literal["bluelab-phase4-pilot-v1"]
    license: str = Field(min_length=20)
    fixtures: list[PilotFixture]

    @model_validator(mode="after")
    def _fixed_scope(self) -> PilotCorpus:
        if len(self.fixtures) != 10:
            raise ValueError("the Phase 4 pilot corpus must contain exactly 10 fixtures")
        ids = [fixture.fixture_id for fixture in self.fixtures]
        if len(set(ids)) != len(ids):
            raise ValueError("fixture ids must be unique")
        if {fixture.call_type for fixture in self.fixtures} != {
            "discovery",
            "post_proposal",
            "renewal",
            "upsell",
        }:
            raise ValueError("the pilot corpus must cover all four call types")
        if sum(fixture.injection_case for fixture in self.fixtures) < 2:
            raise ValueError("the pilot corpus must include injection cross-checks")
        return self

    def verify_hashes(self) -> list[str]:
        mismatches: list[str] = []
        for fixture in self.fixtures:
            if fixture.content_hash != fixture.computed_content_hash():
                mismatches.append(f"{fixture.fixture_id}:content_hash")
            if fixture.transcript_hash != fixture.computed_transcript_hash():
                mismatches.append(f"{fixture.fixture_id}:transcript_hash")
            if (
                fixture.injection_control_transcript_hash
                != fixture.computed_injection_control_transcript_hash()
            ):
                mismatches.append(
                    f"{fixture.fixture_id}:injection_control_transcript_hash"
                )
        return mismatches


def _sha256(value: Any) -> str:
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def load_pilot_corpus(path: Path) -> PilotCorpus:
    try:
        corpus = PilotCorpus.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StudyEvidenceError("pilot corpus is missing or invalid") from exc
    mismatches = corpus.verify_hashes()
    if mismatches:
        raise StudyEvidenceError("pilot corpus hashes do not match fixed content")
    return corpus


def wilson_interval(
    *, successes: int, trials: int, confidence: float = 0.95
) -> tuple[float, float]:
    if trials <= 0 or successes < 0 or successes > trials:
        raise ValueError("Wilson inputs must satisfy 0 <= successes <= trials")
    if confidence != 0.95:
        raise ValueError("the prescribed consistency interval is 95 percent")
    # The requirement is a one-sided lower 95% bound. Returning the matching
    # upper endpoint as well keeps the primitive useful without silently using
    # a two-sided 95% interval (which would apply a stricter 97.5% lower tail).
    z = 1.6448536269514722
    proportion = successes / trials
    denominator = 1 + (z * z / trials)
    center = (proportion + z * z / (2 * trials)) / denominator
    radius = (
        z
        * math.sqrt(
            proportion * (1 - proportion) / trials + z * z / (4 * trials * trials)
        )
        / denominator
    )
    return max(0.0, center - radius), min(1.0, center + radius)


def icc_2_1(ratings: list[list[float]]) -> float:
    """Two-way random-effects, absolute-agreement, single-measure ICC(2,1)."""
    if len(ratings) < 2 or not ratings or len(ratings[0]) < 2:
        raise ValueError("ICC(2,1) requires at least two targets and two raters")
    raters = len(ratings[0])
    if any(len(row) != raters for row in ratings):
        raise ValueError("ICC rating matrix must be rectangular")
    targets = len(ratings)
    grand = fmean(value for row in ratings for value in row)
    row_means = [fmean(row) for row in ratings]
    column_means = [fmean(row[index] for row in ratings) for index in range(raters)]
    ms_rows = raters * sum((value - grand) ** 2 for value in row_means) / (
        targets - 1
    )
    ms_columns = targets * sum((value - grand) ** 2 for value in column_means) / (
        raters - 1
    )
    residual = sum(
        (
            ratings[row][column]
            - row_means[row]
            - column_means[column]
            + grand
        )
        ** 2
        for row in range(targets)
        for column in range(raters)
    )
    ms_error = residual / ((targets - 1) * (raters - 1))
    denominator = (
        ms_rows
        + (raters - 1) * ms_error
        + raters * (ms_columns - ms_error) / targets
    )
    if denominator == 0:
        raise ValueError("ICC(2,1) is undefined for this rating matrix")
    return (ms_rows - ms_error) / denominator


@dataclass(frozen=True, slots=True)
class BlandAltmanResult:
    bias: float
    lower_limit: float
    upper_limit: float


def bland_altman(ai_scores: list[float], human_scores: list[float]) -> BlandAltmanResult:
    if len(ai_scores) != len(human_scores) or len(ai_scores) < 2:
        raise ValueError("Bland-Altman requires two equal series with at least two values")
    differences = [
        ai - human for ai, human in zip(ai_scores, human_scores, strict=True)
    ]
    bias = fmean(differences)
    spread = stdev(differences)
    return BlandAltmanResult(
        bias=bias,
        lower_limit=bias - 1.96 * spread,
        upper_limit=bias + 1.96 * spread,
    )


def validate_blind_expert_evidence(payload: dict[str, Any]) -> None:
    raters = payload.get("raters")
    if not isinstance(raters, list) or len(raters) < 3:
        raise StudyEvidenceError("at least three independent expert raters are required")
    identities = [
        rater.get("rater_id") for rater in raters if isinstance(rater, dict)
    ]
    if len(identities) != len(raters) or len(set(identities)) != len(identities):
        raise StudyEvidenceError("expert rater identities must be distinct")
    if any(not rater.get("blind") for rater in raters):
        raise StudyEvidenceError("every expert rating must be blind to model output")
    if payload.get("evidence_attestation") != "human_expert_ratings_collected":
        raise StudyEvidenceError("human raters must attest the expert evidence")
    ratings = payload.get("ratings")
    if not isinstance(ratings, list) or not ratings:
        raise StudyEvidenceError("expert ratings are incomplete")


class InjectionControlEvidence(BaseModel):
    """One injection-stripped paired run through the same grading pipeline."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    transcript_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_version: str = Field(min_length=1, max_length=100)
    aggregate_source: Literal["server_weighted_dimensions"]
    pipeline_validation: Literal["passed"]
    overall: float = Field(ge=0, le=10)
    dimensions: dict[str, float]


class AIRunEvidence(BaseModel):
    """One production-pipeline evaluation of one fixed corpus fixture."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fixture_id: str = Field(pattern=r"^SYN-[0-9]{3}$")
    run_index: int = Field(ge=1, le=5)
    model_version: str = Field(min_length=1, max_length=100)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    transcript_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    aggregate_source: Literal["server_weighted_dimensions"]
    pipeline_validation: Literal["passed"]
    overall: float = Field(ge=0, le=10)
    dimensions: dict[str, float]
    injection_control: InjectionControlEvidence | None = None


class AIRunCollection(BaseModel):
    """Content-free artifact produced by the real evaluator pipeline."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    collected_at: str = Field(min_length=1)
    runs: list[AIRunEvidence] = Field(min_length=1)


class ExpertRater(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rater_id: str = Field(min_length=1, max_length=100)
    blind: Literal[True]
    egyptian_market_sales_manager: Literal[True]
    credential_attested: Literal[True]


class ExpertRating(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fixture_id: str = Field(pattern=r"^SYN-[0-9]{3}$")
    rater_id: str = Field(min_length=1, max_length=100)
    overall: float = Field(ge=0, le=10)
    dimensions: dict[str, float]


class ExpertPreregistration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    registered_before_collection: Literal[True]
    bias_threshold: float = Field(gt=0, le=10)
    icc_ceiling_margin: float = Field(ge=0, le=2)
    subgroup_bias_threshold: float = Field(gt=0, le=10)


class ExpertStudyEvidence(BaseModel):
    """Closed, attested input contract for the blind-panel analysis."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    preregistration: ExpertPreregistration
    evidence_attestation: Literal["human_expert_ratings_collected"]
    randomized_call_order: Literal[True]
    independent_scoring: Literal[True]
    raters: list[ExpertRater] = Field(min_length=3)
    ratings: list[ExpertRating] = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class ConsistencyStratum:
    call_type: str
    successes: int
    trials: int
    observed_rate: float
    wilson_lower_95: float


@dataclass(frozen=True, slots=True)
class InjectionCheck:
    fixture_id: str
    pipeline_validated: bool
    aggregate_is_server_arithmetic: bool
    all_comparisons_within_tolerance: bool
    all_pairwise_comparisons_within_tolerance: bool
    all_injection_control_comparisons_within_tolerance: bool


@dataclass(frozen=True, slots=True)
class ConsistencyReport:
    complete: bool
    gate_status: Literal["pass", "fail", "insufficient_sample"]
    all_observed_comparisons_within_tolerance: bool
    all_pairwise_comparisons_within_tolerance: bool
    injection_resistant: bool
    pooled_successes: int
    pooled_trials: int
    by_call_type: list[ConsistencyStratum]
    injection_checks: list[InjectionCheck]
    overall_standard_deviation: float
    dimension_standard_deviation: dict[str, float]


@dataclass(frozen=True, slots=True)
class CanaryFixtureResult:
    fixture_id: str
    call_type: str
    overall_delta: float
    maximum_dimension_delta: float
    breached: bool


@dataclass(frozen=True, slots=True)
class CanaryReport:
    breached: bool
    fixtures: list[CanaryFixtureResult]


@dataclass(frozen=True, slots=True)
class AgreementComparison:
    label: str
    ai_human_icc: float
    icc_ci_lower: float
    icc_ci_upper: float
    human_human_icc: float
    human_human_icc_ci_lower: float
    human_human_icc_ci_upper: float
    bias: float
    bias_ci_lower: float
    bias_ci_upper: float
    lower_limit: float
    upper_limit: float
    confidence: float
    escalates: bool


@dataclass(frozen=True, slots=True)
class ExpertAgreementReport:
    complete: bool
    gate_status: Literal["pilot_signal", "escalate"]
    sample_size: int
    human_human_ceiling: float
    human_human_ceiling_ci_lower: float
    human_human_ceiling_ci_upper: float
    bonferroni_confidence: float
    comparisons: list[AgreementComparison]
    demeanor_enabled: Literal[False] = False


def corpus_digest(corpus: PilotCorpus) -> str:
    """Stable digest binding all study artifacts to the exact fixed corpus."""
    return _sha256(corpus.model_dump(mode="json", by_alias=True))


def load_ai_run_collection(path: Path, corpus: PilotCorpus) -> AIRunCollection:
    try:
        collection = AIRunCollection.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StudyEvidenceError("AI run evidence is missing or invalid") from exc
    if collection.corpus_sha256 != corpus_digest(corpus):
        raise StudyEvidenceError("AI run evidence corpus hash does not match")
    _validate_ai_runs(corpus, collection.runs)
    return collection


def load_expert_evidence(path: Path, corpus: PilotCorpus) -> ExpertStudyEvidence:
    try:
        evidence = ExpertStudyEvidence.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StudyEvidenceError("expert evidence is missing or invalid") from exc
    _validate_expert_evidence(corpus, evidence)
    return evidence


def _expected_dimensions(fixture: PilotFixture) -> dict[str, int]:
    result: dict[str, int] = {}
    rubric = fixture.frozen_basis.get("rubric")
    if not isinstance(rubric, list):
        raise StudyEvidenceError(f"{fixture.fixture_id} has no frozen rubric")
    for item in rubric:
        if not isinstance(item, dict):
            raise StudyEvidenceError(f"{fixture.fixture_id} has an invalid frozen rubric")
        dimension_id = item.get("dimension_id")
        weight = item.get("weight")
        if not isinstance(dimension_id, str) or not isinstance(weight, int):
            raise StudyEvidenceError(f"{fixture.fixture_id} has an invalid frozen rubric")
        result[dimension_id] = weight
    if not result or sum(result.values()) != 100:
        raise StudyEvidenceError(f"{fixture.fixture_id} rubric weights must total 100")
    return result


def _validate_score_arithmetic(
    *,
    dimensions: dict[str, float],
    overall: float,
    expected: dict[str, int],
    label: str,
) -> None:
    if set(dimensions) != set(expected):
        raise StudyEvidenceError(f"{label} dimensions do not match the frozen rubric")
    if any(not 0 <= value <= 10 for value in dimensions.values()):
        raise StudyEvidenceError(f"{label} dimension score is outside 0..10")
    weighted = sum(
        Decimal(str(dimensions[key])) * Decimal(weight)
        for key, weight in expected.items()
    ) / Decimal(100)
    expected_overall = float(
        weighted.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    )
    if not math.isclose(overall, expected_overall, abs_tol=1e-9):
        raise StudyEvidenceError(f"{label} overall is not server-weighted arithmetic")


def _validate_ai_runs(
    corpus: PilotCorpus,
    runs: list[AIRunEvidence],
    *,
    accepted_runs_per_fixture: frozenset[int] = frozenset({5}),
    require_injection_controls: bool = True,
) -> dict[str, list[AIRunEvidence]]:
    fixture_by_id = {item.fixture_id: item for item in corpus.fixtures}
    grouped: dict[str, list[AIRunEvidence]] = {key: [] for key in fixture_by_id}
    for run in runs:
        fixture = fixture_by_id.get(run.fixture_id)
        if fixture is None:
            raise StudyEvidenceError("AI run references a fixture outside the fixed corpus")
        if "fixture" in run.model_version.casefold() or "not-validity-evidence" in run.model_version.casefold():
            raise StudyEvidenceError("fixture evaluator output cannot be validity evidence")
        if run.model_version not in _PRESCRIBED_EVALUATOR_MODELS:
            raise StudyEvidenceError(
                "AI run does not use a prescribed evaluator model version"
            )
        if run.content_hash != fixture.content_hash or run.transcript_hash != fixture.transcript_hash:
            raise StudyEvidenceError("AI run hashes do not match the fixed corpus")
        expected = _expected_dimensions(fixture)
        _validate_score_arithmetic(
            dimensions=run.dimensions,
            overall=run.overall,
            expected=expected,
            label="AI run",
        )
        control = run.injection_control
        if not fixture.injection_case and control is not None:
            raise StudyEvidenceError("a non-probe AI run contains injection control data")
        if fixture.injection_case and require_injection_controls and control is None:
            raise StudyEvidenceError("injection probe paired-control evidence is incomplete")
        if control is not None:
            if control.model_version != run.model_version:
                raise StudyEvidenceError(
                    "injection control paired model version does not match"
                )
            if control.transcript_hash != fixture.injection_control_transcript_hash:
                raise StudyEvidenceError(
                    "injection control hash does not match the fixed corpus"
                )
            _validate_score_arithmetic(
                dimensions=control.dimensions,
                overall=control.overall,
                expected=expected,
                label="injection control",
            )
        grouped[run.fixture_id].append(run)
    for fixture_id, fixture_runs in grouped.items():
        run_count = len(fixture_runs)
        if (
            run_count not in accepted_runs_per_fixture
            or {item.run_index for item in fixture_runs}
            != set(range(1, run_count + 1))
        ):
            names = {1: "one", 5: "five"}
            expected_run_counts = " or ".join(
                names.get(value, str(value))
                for value in sorted(accepted_runs_per_fixture)
            )
            raise StudyEvidenceError(
                f"{fixture_id} must have exactly {expected_run_counts} indexed AI runs"
            )
        fixture_runs.sort(key=lambda item: item.run_index)
    if len({run.model_version for run in runs}) != 1:
        raise StudyEvidenceError("AI runs must use one evaluator model version")
    return grouped


def analyze_consistency(
    corpus: PilotCorpus, runs: list[AIRunEvidence]
) -> ConsistencyReport:
    """Apply the pre-registered repeated-measures/Wilson consistency design."""
    grouped = _validate_ai_runs(corpus, runs)
    outcomes: dict[str, list[bool]] = {
        call_type: [] for call_type in ("discovery", "post_proposal", "renewal", "upsell")
    }
    injection: list[InjectionCheck] = []
    pairwise_checks: list[bool] = []
    overall_stdevs: list[float] = []
    dimension_stdevs: dict[str, list[float]] = {}
    for fixture in corpus.fixtures:
        fixture_runs = grouped[fixture.fixture_id]
        reference = fixture_runs[0]
        comparisons: list[bool] = []

        def within(left: AIRunEvidence, right: AIRunEvidence) -> bool:
            return abs(left.overall - right.overall) <= 0.5 and all(
                abs(left.dimensions[key] - right.dimensions[key]) <= 1.0
                for key in left.dimensions
            )

        for candidate in fixture_runs[1:]:
            observed = within(reference, candidate)
            comparisons.append(observed)
            outcomes[fixture.call_type].append(observed)
        pairwise = [
            within(fixture_runs[left], fixture_runs[right])
            for left in range(len(fixture_runs))
            for right in range(left + 1, len(fixture_runs))
        ]
        pairwise_checks.extend(pairwise)
        overall_stdevs.append(stdev(item.overall for item in fixture_runs))
        names = {
            str(item["dimension_id"]): str(item["name"])
            for item in fixture.frozen_basis["rubric"]
        }
        for dimension_id, name in names.items():
            dimension_stdevs.setdefault(name, []).append(
                stdev(item.dimensions[dimension_id] for item in fixture_runs)
            )
        if fixture.injection_case:
            control_comparisons: list[bool] = []
            for run in fixture_runs:
                control = run.injection_control
                if control is None:
                    raise StudyEvidenceError(
                        "injection probe paired-control evidence is incomplete"
                    )
                control_comparisons.append(
                    abs(run.overall - control.overall) <= 0.5
                    and all(
                        abs(run.dimensions[key] - control.dimensions[key]) <= 1.0
                        for key in run.dimensions
                    )
                )
            injection.append(
                InjectionCheck(
                    fixture_id=fixture.fixture_id,
                    pipeline_validated=True,
                    aggregate_is_server_arithmetic=True,
                    all_comparisons_within_tolerance=all(comparisons),
                    all_pairwise_comparisons_within_tolerance=all(pairwise),
                    all_injection_control_comparisons_within_tolerance=all(
                        control_comparisons
                    ),
                )
            )
    strata: list[ConsistencyStratum] = []
    for call_type, values in outcomes.items():
        successes = sum(values)
        lower, _ = wilson_interval(successes=successes, trials=len(values))
        strata.append(
            ConsistencyStratum(
                call_type=call_type,
                successes=successes,
                trials=len(values),
                observed_rate=successes / len(values),
                wilson_lower_95=lower,
            )
        )
    all_observed = all(all(values) for values in outcomes.values())
    injection_resistant = all(
        item.all_injection_control_comparisons_within_tolerance
        for item in injection
    )
    if not all_observed or not injection_resistant:
        gate_status: Literal["pass", "fail", "insufficient_sample"] = "fail"
    elif all(item.wilson_lower_95 >= 0.95 for item in strata):
        gate_status = "pass"
    else:
        gate_status = "insufficient_sample"
    pooled = [value for values in outcomes.values() for value in values]
    return ConsistencyReport(
        complete=True,
        gate_status=gate_status,
        all_observed_comparisons_within_tolerance=all_observed,
        all_pairwise_comparisons_within_tolerance=all(pairwise_checks),
        injection_resistant=injection_resistant,
        pooled_successes=sum(pooled),
        pooled_trials=len(pooled),
        by_call_type=strata,
        injection_checks=injection,
        overall_standard_deviation=fmean(overall_stdevs),
        dimension_standard_deviation={
            name: fmean(values) for name, values in sorted(dimension_stdevs.items())
        },
    )


def compare_canary(
    corpus: PilotCorpus,
    reference_runs: list[AIRunEvidence],
    candidate_runs: list[AIRunEvidence],
) -> CanaryReport:
    """Compare two real, corpus-bound panels using the NFR-004 tripwire."""
    reference = _validate_ai_runs(corpus, reference_runs)
    candidate = _validate_ai_runs(
        corpus,
        candidate_runs,
        accepted_runs_per_fixture=frozenset({1, 5}),
        require_injection_controls=False,
    )
    fixtures: list[CanaryFixtureResult] = []
    for fixture in corpus.fixtures:
        baseline = reference[fixture.fixture_id][0]
        observed = candidate[fixture.fixture_id][0]
        overall_delta = abs(observed.overall - baseline.overall)
        dimension_delta = max(
            abs(observed.dimensions[key] - baseline.dimensions[key])
            for key in baseline.dimensions
        )
        fixtures.append(
            CanaryFixtureResult(
                fixture_id=fixture.fixture_id,
                call_type=fixture.call_type,
                overall_delta=overall_delta,
                maximum_dimension_delta=dimension_delta,
                breached=overall_delta > 0.5 or dimension_delta > 1.0,
            )
        )
    return CanaryReport(
        breached=any(item.breached for item in fixtures), fixtures=fixtures
    )


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise StudyEvidenceError("confidence interval could not be estimated")
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _bootstrap_interval(
    rows: list[list[float]],
    statistic: Callable[[list[list[float]]], float],
    *,
    confidence: float,
    samples: int,
    seed_label: str,
) -> tuple[float, float]:
    if samples < 100:
        raise ValueError("at least 100 deterministic bootstrap samples are required")
    seed = int.from_bytes(hashlib.sha256(seed_label.encode()).digest()[:8], "big")
    rng = random.Random(seed)
    estimates: list[float] = []
    for _ in range(samples):
        sample = [rows[rng.randrange(len(rows))] for _ in rows]
        try:
            estimate = statistic(sample)
        except ValueError:
            continue
        if math.isfinite(estimate):
            estimates.append(estimate)
    alpha = 1 - confidence
    return _percentile(estimates, alpha / 2), _percentile(estimates, 1 - alpha / 2)


def _validate_expert_evidence(
    corpus: PilotCorpus, evidence: ExpertStudyEvidence
) -> dict[tuple[str, str], ExpertRating]:
    if evidence.corpus_sha256 != corpus_digest(corpus):
        raise StudyEvidenceError("expert evidence corpus hash does not match")
    rater_ids = [item.rater_id for item in evidence.raters]
    if len(set(rater_ids)) != len(rater_ids):
        raise StudyEvidenceError("expert rater identities must be distinct")
    fixture_by_id = {item.fixture_id: item for item in corpus.fixtures}
    expected_pairs = {
        (fixture_id, rater_id)
        for fixture_id in fixture_by_id
        for rater_id in rater_ids
    }
    rating_by_pair: dict[tuple[str, str], ExpertRating] = {}
    for rating in evidence.ratings:
        key = (rating.fixture_id, rating.rater_id)
        fixture = fixture_by_id.get(rating.fixture_id)
        if fixture is None or rating.rater_id not in rater_ids or key in rating_by_pair:
            raise StudyEvidenceError("expert ratings contain an unknown or duplicate assignment")
        if set(rating.dimensions) != set(_expected_dimensions(fixture)):
            raise StudyEvidenceError("expert rating dimensions do not match the frozen rubric")
        if any(not 0 <= value <= 10 for value in rating.dimensions.values()):
            raise StudyEvidenceError("expert dimension score is outside 0..10")
        rating_by_pair[key] = rating
    if set(rating_by_pair) != expected_pairs:
        raise StudyEvidenceError("expert ratings are incomplete")
    return rating_by_pair


def analyze_expert_agreement(
    corpus: PilotCorpus,
    runs: list[AIRunEvidence],
    evidence: ExpertStudyEvidence,
    *,
    bootstrap_samples: int = 5_000,
) -> ExpertAgreementReport:
    """Compute ICC(2,1), Bland-Altman, human ceiling, and corrected CIs."""
    grouped = _validate_ai_runs(corpus, runs)
    rating_by_pair = _validate_expert_evidence(corpus, evidence)
    rater_ids = [item.rater_id for item in evidence.raters]
    human_rows = [
        [rating_by_pair[(fixture.fixture_id, rater)].overall for rater in rater_ids]
        for fixture in corpus.fixtures
    ]
    human_ceiling = icc_2_1(human_rows)

    dimensions = sorted(
        {
            str(item["name"])
            for fixture in corpus.fixtures
            for item in fixture.frozen_basis["rubric"]
        }
    )
    specifications: list[tuple[str, str | None, str | None]] = [("overall", None, None)]
    specifications.extend((f"dimension:{name}", None, name) for name in dimensions)
    for call_type in ("discovery", "post_proposal", "renewal", "upsell"):
        specifications.append((f"call_type:{call_type}:overall", call_type, None))
        call_type_dimensions = sorted(
            {
                str(item["name"])
                for fixture in corpus.fixtures
                if fixture.call_type == call_type
                for item in fixture.frozen_basis["rubric"]
            }
        )
        specifications.extend(
            (f"call_type:{call_type}:dimension:{name}", call_type, name)
            for name in call_type_dimensions
        )
    corrected_confidence = 1 - (0.05 / len(specifications))
    comparisons: list[AgreementComparison] = []
    prereg = evidence.preregistration

    for label, comparison_call_type, dimension_name in specifications:
        selected = [
            fixture
            for fixture in corpus.fixtures
            if (
                comparison_call_type is None
                or fixture.call_type == comparison_call_type
            )
            and (
                dimension_name is None
                or any(
                    item["name"] == dimension_name
                    for item in fixture.frozen_basis["rubric"]
                )
            )
        ]
        ai_scores: list[float] = []
        human_scores: list[float] = []
        comparison_human_rows: list[list[float]] = []
        for fixture in selected:
            ai = grouped[fixture.fixture_id][0]
            human = [rating_by_pair[(fixture.fixture_id, rater)] for rater in rater_ids]
            if dimension_name is None:
                ai_scores.append(ai.overall)
                human_row = [item.overall for item in human]
            else:
                dimension_id = next(
                    str(item["dimension_id"])
                    for item in fixture.frozen_basis["rubric"]
                    if item["name"] == dimension_name
                )
                ai_scores.append(ai.dimensions[dimension_id])
                human_row = [item.dimensions[dimension_id] for item in human]
            comparison_human_rows.append(human_row)
            human_scores.append(fmean(human_row))
        rows = [[ai, human] for ai, human in zip(ai_scores, human_scores, strict=True)]
        ai_human_icc = icc_2_1(rows)
        agreement = bland_altman(ai_scores, human_scores)
        icc_low, icc_high = _bootstrap_interval(
            rows,
            icc_2_1,
            confidence=corrected_confidence,
            samples=bootstrap_samples,
            seed_label=f"icc:{label}",
        )
        comparison_human_ceiling = icc_2_1(comparison_human_rows)
        human_icc_low, human_icc_high = _bootstrap_interval(
            comparison_human_rows,
            icc_2_1,
            confidence=corrected_confidence,
            samples=bootstrap_samples,
            seed_label=f"human-icc:{label}",
        )
        differences = [[ai - human] for ai, human in zip(ai_scores, human_scores, strict=True)]
        bias_low, bias_high = _bootstrap_interval(
            differences,
            lambda values: fmean(item[0] for item in values),
            confidence=corrected_confidence,
            samples=bootstrap_samples,
            seed_label=f"bias:{label}",
        )
        bias_threshold = (
            prereg.subgroup_bias_threshold
            if comparison_call_type is not None
            else prereg.bias_threshold
        )
        escalates = (
            abs(agreement.bias) > bias_threshold
            or ai_human_icc
            < comparison_human_ceiling - prereg.icc_ceiling_margin
        )
        comparisons.append(
            AgreementComparison(
                label=label,
                ai_human_icc=ai_human_icc,
                icc_ci_lower=icc_low,
                icc_ci_upper=icc_high,
                human_human_icc=comparison_human_ceiling,
                human_human_icc_ci_lower=human_icc_low,
                human_human_icc_ci_upper=human_icc_high,
                bias=agreement.bias,
                bias_ci_lower=bias_low,
                bias_ci_upper=bias_high,
                lower_limit=agreement.lower_limit,
                upper_limit=agreement.upper_limit,
                confidence=corrected_confidence,
                escalates=escalates,
            )
        )
    overall_comparison = next(item for item in comparisons if item.label == "overall")
    return ExpertAgreementReport(
        complete=True,
        gate_status=(
            "escalate" if any(item.escalates for item in comparisons) else "pilot_signal"
        ),
        sample_size=len(corpus.fixtures),
        human_human_ceiling=human_ceiling,
        human_human_ceiling_ci_lower=overall_comparison.human_human_icc_ci_lower,
        human_human_ceiling_ci_upper=overall_comparison.human_human_icc_ci_upper,
        bonferroni_confidence=corrected_confidence,
        comparisons=comparisons,
    )
