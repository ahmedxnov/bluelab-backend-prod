"""Phase 4 content-free operational signal contract."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest

from bluelab.adapters.evaluator_llm import EvaluationCall
from bluelab.modules.review.grading import EvaluationValidationError
from bluelab.platform.config import Settings
from bluelab.platform.queue.compat import JobEnvelope
from bluelab.platform.telemetry import metrics
from bluelab.work import grade_attempt

pytestmark = [pytest.mark.l1_unit]


@pytest.mark.verifies("NFR-004", "NFR-005", "NFR-006", "SEC-038")
def test_phase4_metric_emitters_use_only_bounded_content_free_labels(monkeypatch) -> None:
    observed: list[tuple[str, float, dict[str, str]]] = []
    monkeypatch.setattr(
        metrics.grading_cost_usd,
        "record",
        lambda value, labels: observed.append(("cost", value, labels)),
    )
    monkeypatch.setattr(
        metrics.grading_consistency_delta,
        "record",
        lambda value, labels: observed.append(("delta", value, labels)),
    )
    monkeypatch.setattr(
        metrics.playback_access,
        "add",
        lambda value, labels: observed.append(("playback", value, labels)),
    )

    metrics.record_grading_cost(cost_usd=Decimal("0.075"))
    metrics.record_grading_consistency(
        delta=0.4, score_level="dimension", call_type="renewal"
    )
    metrics.record_playback_access(outcome="unavailable")

    assert observed == [
        ("cost", 0.075, {}),
        (
            "delta",
            0.4,
            {"call_type": "renewal", "participant_kind": "synthetic", "score_level": "dimension"},
        ),
        ("playback", 1, {"outcome": "unavailable"}),
    ]
    assert all(
        "attempt" not in key and "transcript" not in key and "content" not in key
        for _, _, labels in observed
        for key in labels
    )


def test_metric_emitters_reject_unbounded_label_values() -> None:
    with pytest.raises(ValueError, match="score level"):
        metrics.record_grading_consistency(
            delta=0.1, score_level="Discovery and listening", call_type="renewal"
        )
    with pytest.raises(ValueError, match="playback"):
        metrics.record_playback_access(outcome="attempt-0199")


@pytest.mark.verifies("NFR-006", "SEC-023")
async def test_evaluator_spend_and_version_are_observed_before_output_validation(
    monkeypatch,
) -> None:
    attempt_id = UUID("01900000-0000-7000-8000-000000000499")
    org_id = UUID("01900000-0000-7000-8000-000000000401")

    class Provider:
        async def evaluate(self, _basis):
            return EvaluationCall(
                output=object(),  # type: ignore[arg-type]
                model_version="claude-sonnet-5",
                input_tokens=100,
                output_tokens=50,
                cost_usd=Decimal("0.0007"),
            )

    async def work_item(_session, *, attempt_id):
        return SimpleNamespace(basis=object(), concealed_fragments=())

    def reject_output(*_args, **_kwargs):
        raise EvaluationValidationError("invalid provider output")

    observed: list[tuple[str, object]] = []
    monkeypatch.setattr(grade_attempt.attempts, "grading_work_item", work_item)
    monkeypatch.setattr(grade_attempt, "validate_evaluation", reject_output)
    monkeypatch.setattr(
        metrics,
        "record_grading_cost",
        lambda *, cost_usd: observed.append(("cost", cost_usd)),
    )
    monkeypatch.setattr(
        metrics,
        "record_model_version",
        lambda *, capability, model_version: observed.append(
            (capability, model_version)
        ),
    )

    worker = grade_attempt.registration(
        Settings(VENDOR_FIXTURE_MODE=True),  # type: ignore[call-arg]
        provider=Provider(),
    )
    with pytest.raises(EvaluationValidationError, match="invalid provider output"):
        await worker.handler(
            object(),  # type: ignore[arg-type]
            JobEnvelope(
                version=1,
                org_id=org_id,
                team_id=None,
                args={"attempt_id": str(attempt_id)},
            ),
        )

    assert observed == [
        ("cost", Decimal("0.0007")),
        ("evaluator", "claude-sonnet-5"),
    ]
