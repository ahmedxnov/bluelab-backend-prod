"""Canonical Phase 4 Review projection and playback-selection tests."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest

from bluelab.modules.review.projection import (
    moment_view,
    pinned_playback,
    rubric_view,
)

pytestmark = [pytest.mark.l1_unit]

DIMENSION = UUID("01900000-0000-7000-8000-000000000411")
AMBER = UUID("01900000-0000-7000-8000-000000000412")
RED_LATE = UUID("01900000-0000-7000-8000-000000000413")
RED_FIRST = UUID("01900000-0000-7000-8000-000000000414")


@pytest.mark.verifies("FR-SCR-005", "FR-SCR-017", "AC-SCR-003")
def test_non_author_projection_omits_weight_instead_of_serializing_null() -> None:
    row = {
        "dimension_id": DIMENSION,
        "name": "Discovery",
        "score": Decimal("6.0"),
        "band": "amber",
        "note": "Ask one more diagnostic question.",
        "weight": 35,
    }

    participant = rubric_view(row, reveal_weight=False).model_dump(mode="json")
    author = rubric_view(row, reveal_weight=True).model_dump(mode="json")

    assert "weight" not in participant
    assert author["weight"] == 35
    assert participant["band"] == "amber"


@pytest.mark.verifies("FR-SCR-007", "FR-SCR-008")
def test_moment_projection_preserves_verbatim_arabic_and_server_band() -> None:
    projected = moment_view(
        {
            "id": RED_FIRST,
            "at_ms": 2_000,
            "severity": "red",
            "dimension_id": DIMENSION,
            "transcript_seq": 4,
            "quote": "التغطية مضمونة.",
            "try_instead": "State the underwriting condition.",
            "why_it_matters": "The buyer needs an accurate commitment.",
        }
    )

    assert projected.quote == "التغطية مضمونة."
    assert projected.severity == "red"


@pytest.mark.verifies("FR-SCR-011", "FR-SCR-013")
def test_playback_pins_first_red_then_amber_and_survives_missing_audio() -> None:
    moments = [
        moment_view(
            {
                "id": AMBER,
                "at_ms": 1_000,
                "severity": "amber",
                "dimension_id": DIMENSION,
                "transcript_seq": 1,
                "quote": "سؤال عام.",
                "try_instead": "Ask a focused question.",
                "why_it_matters": "It reveals the buyer's need.",
            }
        ),
        moment_view(
            {
                "id": RED_LATE,
                "at_ms": 8_000,
                "severity": "red",
                "dimension_id": DIMENSION,
                "transcript_seq": 3,
                "quote": "وعد غير دقيق.",
                "try_instead": "Qualify the commitment.",
                "why_it_matters": "Accuracy protects buyer trust.",
            }
        ),
        moment_view(
            {
                "id": RED_FIRST,
                "at_ms": 5_000,
                "severity": "red",
                "dimension_id": DIMENSION,
                "transcript_seq": 2,
                "quote": "ضمان كامل.",
                "try_instead": "Name the applicable condition.",
                "why_it_matters": "The proposal is conditional.",
            }
        ),
    ]

    playback = pinned_playback(
        moments=moments,
        recording_status="unavailable",
        recording_url=None,
    )

    assert playback.pinned_moment_id == RED_FIRST
    assert playback.open_at_ms == 5_000
    assert playback.recording_status == "unavailable"
    assert playback.recording_url is None
