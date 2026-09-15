"""Analyze Phase 4 consistency and blind-expert evidence without softening gates."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from bluelab.modules.review.validity import (
    ConsistencyReport,
    ExpertAgreementReport,
    StudyEvidenceError,
    analyze_consistency,
    analyze_expert_agreement,
    load_ai_run_collection,
    load_expert_evidence,
    load_pilot_corpus,
)


def phase4_pilot_outcome(
    consistency: ConsistencyReport,
    agreement: ExpertAgreementReport,
) -> Literal["pilot_signal", "escalate"]:
    """Close the small pilot without misreporting full-power validation."""
    if not consistency.complete or not agreement.complete:
        raise StudyEvidenceError("Phase 4 pilot evidence is incomplete")
    if consistency.gate_status == "fail" or agreement.gate_status == "escalate":
        return "escalate"
    if (
        consistency.gate_status == "insufficient_sample"
        and agreement.gate_status == "pilot_signal"
    ):
        return "pilot_signal"
    raise StudyEvidenceError("Phase 4 pilot produced an impossible gate combination")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus", type=Path, default=Path("tests/fixtures/grading/corpus.v1.json")
    )
    parser.add_argument("--ai-runs", type=Path, required=True)
    parser.add_argument("--expert-ratings", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--bootstrap-samples", type=int, default=5_000)
    return parser.parse_args()


def _write(report: dict[str, Any], output: Path | None) -> None:
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if output is None:
        print(rendered, end="")
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8", newline="\n")


def main() -> int:
    args = _arguments()
    try:
        corpus = load_pilot_corpus(args.corpus)
        ai = load_ai_run_collection(args.ai_runs, corpus)
        consistency = analyze_consistency(corpus, ai.runs)
        report: dict[str, Any] = {
            "schema_version": 1,
            "consistency": asdict(consistency),
            "demeanor_gate": {
                "enabled": False,
                "status": "safe_default_off_pending_fairness_validation",
            },
        }
        if args.expert_ratings is None:
            report["expert_agreement"] = {
                "complete": False,
                "status": "blocked_missing_blind_human_evidence",
            }
            report["overall_status"] = "blocked"
            _write(report, args.output)
            return 2
        experts = load_expert_evidence(args.expert_ratings, corpus)
        agreement = analyze_expert_agreement(
            corpus,
            ai.runs,
            experts,
            bootstrap_samples=args.bootstrap_samples,
        )
        report["expert_agreement"] = asdict(agreement)
        report["overall_status"] = phase4_pilot_outcome(consistency, agreement)
        _write(report, args.output)
        return 0 if report["overall_status"] == "pilot_signal" else 2
    except StudyEvidenceError as exc:
        _write(
            {
                "schema_version": 1,
                "overall_status": "invalid_evidence",
                "reason": str(exc),
            },
            args.output,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
