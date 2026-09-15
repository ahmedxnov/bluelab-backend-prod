"""Candidate-safe request and response models (no evaluation fields)."""
from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class PreflightChecks(BaseModel):
    microphone: bool
    audio_output: bool
    connection: bool
class PreflightSubmit(BaseModel):
    checks: PreflightChecks
    consent: bool
    terms_accepted: bool
class AssessmentStageView(BaseModel):
    stage_id: UUID
    ord: int
    status: Literal["completed", "next", "upcoming", "closed_incomplete"]
    call_type: str
    label: str | None
    restart_available: bool
class AssessmentView(BaseModel):
    candidate_name: str
    position_title: str
    org_name: str
    language: Literal["ar-EG"] = "ar-EG"
    state: Literal["preflight_required", "ready", "in_progress", "completed"]
    gates: dict[str, bool]
    progress: dict[str, int]
    stages: list[AssessmentStageView]
    completion: dict[str, datetime | int] | None


class StageBriefView(BaseModel):
    """Candidate projection of the standard brief, deliberately evaluation-free."""

    drill_id: UUID
    label: str
    call_type: str
    lead_type: str | None = None
    language: Literal["ar-EG"] = "ar-EG"
    buyer: dict[str, object]
    context: str
    product_summary: str
    stage: dict[str, int]


class CandidateReferenceView(BaseModel):
    drill_id: UUID
    frozen: bool
    groups: list[dict[str, object]]
    provenance: dict[str, object]
