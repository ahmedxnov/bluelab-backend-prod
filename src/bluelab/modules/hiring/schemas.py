"""Request and manager-only response shapes for the hiring surface."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

ReportPolicy = Literal["after_finish", "rejected_only", "withhold"]
PipelineView = Literal["pipeline", "review_queue", "approved"]


class PositionCreate(BaseModel):
    title: str = Field(min_length=1)
    openings: int = Field(ge=1)
    notify_on_completion: bool = False
    report_policy: ReportPolicy = "rejected_only"


class PositionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1)
    openings: int | None = Field(default=None, ge=1)
    notify_on_completion: bool | None = None
    report_policy: ReportPolicy | None = None
    invite_expiry_days: int | None = Field(default=None, ge=1, le=60)
    invite_template: str | None = Field(default=None, max_length=20_000)


class AssessmentPut(BaseModel):
    drill_ids: list[UUID] = Field(min_length=1)


class CandidateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=500)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=100)
    linkedin: str | None = Field(default=None, max_length=500)
    source: str | None = Field(default=None, max_length=500)
    internal_note: str | None = Field(default=None, max_length=4000)


class CandidateBatch(BaseModel):
    candidates: list[CandidateCreate] = Field(min_length=1, max_length=100)


class InviteBatch(BaseModel):
    candidate_ids: list[UUID] = Field(min_length=1, max_length=100)
    expiry_days: int | None = Field(default=None, ge=1, le=60)
    invite_template: str | None = Field(default=None, max_length=20_000)


class CandidatePatch(BaseModel):
    internal_note: str | None = Field(max_length=4000)


class PositionStage(BaseModel):
    stage_id: UUID
    ord: int
    drill: dict[str, object]


class PositionDetail(BaseModel):
    id: UUID
    title: str
    openings: int
    status: str
    counts: dict[str, int]
    created_at: datetime
    notify_on_completion: bool
    report_policy: ReportPolicy
    invite_expiry_days: int
    invite_template: str | None
    language: Literal["ar-EG"] = "ar-EG"
    assessment_frozen: bool
    stages: list[PositionStage]
    closed_at: datetime | None = None


class PositionSummary(BaseModel):
    id: UUID
    title: str
    openings: int
    status: str
    counts: dict[str, int]
    created_at: datetime


class PositionList(BaseModel):
    data: list[PositionSummary]
    totals: dict[str, int]
    pagination: dict[str, object]


class ParsedCandidateRow(BaseModel):
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    linkedin: str | None = None
    source: str | None = None
    problems: list[str]


class ParseResult(BaseModel):
    rows: list[ParsedCandidateRow]


class PipelineCandidate(BaseModel):
    candidate_id: UUID
    name: str
    email: EmailStr
    phone: str | None = None
    linkedin: str | None = None
    source: str | None = None
    internal_note: str | None = None
    journey_state: Literal["invited", "in_progress", "completed"]
    expired_incomplete: bool
    decision: Literal["pending", "approved", "rejected"]
    decision_frozen: bool
    invite: dict[str, object] | None = None
    overall: dict[str, object] | None = None
    any_restart: bool


class PipelinePage(BaseModel):
    view: PipelineView
    counts: dict[str, int]
    data: list[PipelineCandidate]
    pagination: dict[str, object]


class CandidateDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approved", "rejected"]


class HrContactCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr
    label: str | None = Field(default=None, max_length=200)


class ShortlistSend(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_ids: list[UUID] = Field(min_length=1, max_length=100)
    recipients: list[EmailStr] = Field(min_length=1, max_length=50)
    body: str = Field(min_length=1, max_length=20_000)
    recipients_confirmed: Literal[True]
    acknowledged_new_domains: list[str] = Field(max_length=50)
