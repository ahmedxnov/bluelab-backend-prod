"""Pydantic request and response models for this module's slice of the contract.

The contract is `api/openapi.yaml` and it is authoritative (ADR-0035): these
models are checked against it by the conformance diff, they do not define it.
`snake_case` fields, singular per glossary (api/00 §2).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from bluelab.platform.security.text import safe_plain_text

CallType = Literal["discovery", "post_proposal", "renewal", "upsell"]
LeadType = Literal["inbound_quote", "referral", "cold_outreach"]
EntrySource = Literal["library", "custom"]
GenerationStatus = Literal["none", "running", "succeeded", "failed"]


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConcealedEntryInput(StrictInput):
    source: EntrySource
    option_id: UUID | None = None
    label: str | None = Field(default=None, max_length=500)

    @field_validator("label")
    @classmethod
    def normalize_label(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = safe_plain_text(value).strip()
        if not normalized:
            raise ValueError("must contain visible text")
        return normalized

    @model_validator(mode="after")
    def exact_source_value(self) -> ConcealedEntryInput:
        if self.source == "library" and (
            self.option_id is None or self.label is not None
        ):
            raise ValueError("library entries require option_id only")
        if self.source == "custom" and (
            self.option_id is not None or not (self.label or "").strip()
        ):
            raise ValueError("custom entries require label only")
        return self


class DrillInputs(StrictInput):
    call_type: CallType
    lead_type: LeadType | None = None
    challenges: list[ConcealedEntryInput] = Field(max_length=20)
    hidden_motives: list[ConcealedEntryInput] = Field(max_length=20)

    @model_validator(mode="after")
    def lead_matches_call_type(self) -> DrillInputs:
        if (self.call_type == "discovery") != (self.lead_type is not None):
            raise ValueError("lead_type is required only for discovery")
        return self


class RubricGenerationRequest(StrictInput):
    discard_confirmed: bool | None = None


class WeightPatchItem(StrictInput):
    dimension_id: UUID
    weight: int = Field(ge=0, le=100)


class WeightPatch(StrictInput):
    weights: list[WeightPatchItem] = Field(min_length=1)


class AuthoringOption(BaseModel):
    id: UUID
    kind: Literal["challenge", "hidden_motive"]
    label: str


class AuthoringOptionList(BaseModel):
    data: list[AuthoringOption]


class ConcealedEntryView(BaseModel):
    source: EntrySource
    label: str


class Persona(BaseModel):
    name: str
    role: str
    company: str
    meta_facts: list[str]


class ScenarioView(BaseModel):
    persona: Persona
    context: str
    product_references: list[str]


class RubricDimensionView(BaseModel):
    id: UUID
    ord: int = Field(ge=1)
    name: str
    weight: int = Field(ge=0, le=100)
    rationale: str


class RubricView(BaseModel):
    dimensions: list[RubricDimensionView]
    total: int


class GenerationState(BaseModel):
    scenario_status: GenerationStatus
    rubric_status: GenerationStatus
    error: str | None = None


class DrillFull(BaseModel):
    id: UUID
    status: Literal["draft", "published", "archived"]
    self_authored: bool
    author_account_id: UUID
    call_type: CallType
    lead_type: LeadType | None = None
    language: Literal["ar-EG"]
    label: str | None
    inputs: dict[str, list[ConcealedEntryView]]
    scenario: ScenarioView | None
    rubric: RubricView | None
    generation: GenerationState
    created_at: datetime
    updated_at: datetime
    published_at: datetime | None = None
    archived_at: datetime | None = None


class GenerationAccepted(BaseModel):
    status: Literal["queued"] = "queued"


class BriefView(BaseModel):
    drill_id: UUID
    label: str
    call_type: CallType
    lead_type: LeadType | None = None
    language: Literal["ar-EG"]
    buyer: Persona
    context: str
    product_summary: str
    author_recap: dict[str, list[str]] | None = None


class ReferenceFact(BaseModel):
    label: str
    value: str
    note: str | None = None


class ReferenceGroup(BaseModel):
    document_title: str
    facts: list[ReferenceFact]


class ReferenceProvenance(BaseModel):
    source_documents: list[str]
    published_by: str
    fact_count: int = Field(ge=0)
    snapshot_at: datetime | None = None


class ReferenceView(BaseModel):
    drill_id: UUID
    frozen: bool
    groups: list[ReferenceGroup]
    provenance: ReferenceProvenance


class GeneratedScenario(StrictInput):
    label: str = Field(min_length=1, max_length=200)
    persona: Persona
    context: str = Field(min_length=1, max_length=8000)
    product_references: list[str] = Field(max_length=100)


class GeneratedRubricDimension(StrictInput):
    name: str = Field(min_length=1, max_length=200)
    weight: int = Field(ge=0, le=100)
    rationale: str = Field(min_length=1, max_length=4000)


class GeneratedRubric(StrictInput):
    dimensions: list[GeneratedRubricDimension] = Field(min_length=1, max_length=20)


class RelevanceResult(StrictInput):
    relevant: bool
    reason: str = Field(min_length=1, max_length=500)


Snapshot = dict[str, Any]
