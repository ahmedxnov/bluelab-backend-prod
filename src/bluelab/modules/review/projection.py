"""The concealment projection — the single enforcement point for FR-SCR-017.

Two representations exist for concealment-bearing resources, chosen *server-side*
by authorship (api/00 §4.3):

  * the full basis (inputs, challenges, hidden motives, rubric with weights) —
    the drill's author, and for team drills the owning team's manager;
  * the participant-safe projection — everyone else eligible.

`rubric_breakdown[].weight` is **absent, not null**, for non-authors
(AC-SCR-003). Hidden motives and challenges are never revealed to a non-author,
in either product, even after the attempt ends. Rows are filtered by RLS;
columns are filtered here (ADR-0031 §4).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from bluelab.modules.review.schemas import (
    Band,
    MomentView,
    PlaybackView,
    RecordingStatus,
    RubricBreakdownItem,
)


def rubric_view(
    row: Mapping[str, Any], *, reveal_weight: bool
) -> RubricBreakdownItem:
    """Project one score and make weight absence a serialization property."""
    return RubricBreakdownItem(
        dimension_id=row["dimension_id"],
        name=str(row["name"]),
        score=float(row["score"]),
        band=cast(Band, row["band"]),
        note=row["note"],
        weight=int(row["weight"]) if reveal_weight else None,
    )


def moment_view(row: Mapping[str, Any]) -> MomentView:
    return MomentView(
        id=row["id"],
        at_ms=int(row["at_ms"]),
        severity=cast(Band, row["severity"]),
        dimension_id=row["dimension_id"],
        transcript_seq=row["transcript_seq"],
        quote=row["quote"],
        try_instead=row["try_instead"],
        why_it_matters=row["why_it_matters"],
    )


def pinned_playback(
    *,
    moments: list[MomentView],
    recording_status: RecordingStatus,
    recording_url: str | None,
) -> PlaybackView:
    """Pin first red, else first amber, else call start (FR-SCR-011)."""
    ordered = sorted(moments, key=lambda item: (item.at_ms, str(item.id)))
    pinned = next((item for item in ordered if item.severity == "red"), None)
    if pinned is None:
        pinned = next((item for item in ordered if item.severity == "amber"), None)
    return PlaybackView(
        recording_status=recording_status,
        recording_url=recording_url if recording_status == "available" else None,
        open_at_ms=pinned.at_ms if pinned is not None else 0,
        pinned_moment_id=pinned.id if pinned is not None else None,
    )
