"""Typed wire models for the Phase 3 knowledge contract."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from bluelab.platform.security.text import safe_plain_text


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FactInput(StrictInput):
    label: str = Field(min_length=1, max_length=200)
    value: str = Field(min_length=1, max_length=4000)
    note: str | None = Field(default=None, max_length=4000)

    @field_validator("label", "value", "note")
    @classmethod
    def normalize_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = safe_plain_text(value).strip()
        if not normalized:
            raise ValueError("must contain visible text")
        return normalized


class CreateDocument(StrictInput):
    title: str = Field(min_length=1, max_length=200)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        normalized = safe_plain_text(value).strip()
        if not normalized:
            raise ValueError("must contain visible text")
        return normalized


class ManualDraft(StrictInput):
    facts: list[FactInput] = Field(min_length=1, max_length=500)


class PublishDocument(StrictInput):
    based_on_version: int = Field(ge=0)


class DraftMarker(BaseModel):
    source: Literal["upload", "manual"]
    status: Literal["extracting", "pending_review", "failed"]
    failure_reason: str | None = None
    created_at: datetime


class DocumentSummary(BaseModel):
    id: UUID
    title: str
    live_fact_count: int = Field(ge=0)
    live_version: int = Field(ge=0)
    draft: DraftMarker | None
    updated_at: datetime


class FactView(BaseModel):
    id: UUID
    ord: int = Field(ge=1)
    label: str
    value: str
    note: str | None


class UploadView(BaseModel):
    upload_id: UUID
    filename: str
    status: Literal["received", "extracting", "extracted", "failed"]
    failure_reason: str | None = None
    created_at: datetime


class DocumentDetail(DocumentSummary):
    live_facts: list[FactView]
    latest_upload: UploadView | None = None


class PriorFact(BaseModel):
    label: str
    value: str
    note: str | None


class ReviewEntry(BaseModel):
    status: Literal["changed", "added", "removed", "unchanged"]
    label: str
    value: str
    note: str | None
    prior: PriorFact | None = None


class ReviewDiff(BaseModel):
    document_id: UUID
    based_on_version: int = Field(ge=0)
    warning: str
    entries: list[ReviewEntry]


class UploadAccepted(BaseModel):
    upload_id: UUID
    status: Literal["received"] = "received"


class DocumentList(BaseModel):
    data: list[DocumentSummary]
