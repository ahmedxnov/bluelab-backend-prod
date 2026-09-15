"""Pydantic request and response models for this module's slice of the contract.

The contract is `api/openapi.yaml` and it is authoritative (ADR-0035): these
models are checked against it by the conformance diff, they do not define it.
`snake_case` fields, singular per glossary (api/00 §2).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field

Band = Literal["green", "amber", "red"]
CallType = Literal["discovery", "post_proposal", "renewal", "upsell"]
AttemptStatus = Literal[
    "in_progress", "completed", "grading_pending", "graded", "interrupted"
]
RecordingStatus = Literal["pending", "available", "unavailable", "erased", "none"]


class DrillSummary(BaseModel):
    id: UUID
    label: str
    call_type: CallType


class AttemptView(BaseModel):
    id: UUID
    drill: DrillSummary
    status: AttemptStatus
    restart: bool
    started_at: datetime
    ended_at: datetime | None = None
    duration_seconds: Annotated[int | None, Field(default=None, ge=0)]
    review_ready: bool


class ReviewContext(BaseModel):
    viewer: Literal["own", "replay"]
    participant_name: str | None = None
    attempt_number: int | None = None


class BuyerView(BaseModel):
    name: str
    role: str
    company: str


class ScoreBand(BaseModel):
    score: Annotated[float, Field(ge=0, le=10)]
    band: Band


class RubricBreakdownItem(BaseModel):
    dimension_id: UUID
    name: str
    score: Annotated[float, Field(ge=0, le=10)]
    band: Band
    note: str | None
    weight: Annotated[
        int | None,
        Field(default=None, ge=0, le=100, exclude_if=lambda value: value is None),
    ]


class MomentView(BaseModel):
    id: UUID
    at_ms: Annotated[int, Field(ge=0)]
    severity: Band
    dimension_id: UUID
    transcript_seq: int | None = None
    quote: str | None = None
    try_instead: str | None = None
    why_it_matters: str | None = None


class TranscriptEntryView(BaseModel):
    seq: Annotated[int, Field(ge=1)]
    speaker: Literal["participant", "buyer"]
    at_ms: Annotated[int, Field(ge=0)]
    text: str


class PlaybackView(BaseModel):
    recording_status: RecordingStatus
    recording_url: str | None
    open_at_ms: Annotated[int, Field(ge=0)]
    pinned_moment_id: UUID | None


class ReviewView(BaseModel):
    attempt_id: UUID
    context: ReviewContext
    drill: DrillSummary
    buyer: BuyerView
    duration_seconds: Annotated[int, Field(ge=0)]
    overall: ScoreBand
    takeaway: str | None
    rubric_breakdown: list[RubricBreakdownItem]
    moments: list[MomentView]
    transcript: list[TranscriptEntryView]
    playback: PlaybackView
