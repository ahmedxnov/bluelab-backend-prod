"""Pydantic request and response models for this module's slice of the contract.

The contract is `api/openapi.yaml` and it is authoritative (ADR-0035): these
models are checked against it by the conformance diff, they do not define it.
`snake_case` fields, singular per glossary (api/00 §2).
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Annotated, Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)

from bluelab.platform.security.passwords import MAX_LENGTH

Role = Literal["manager", "rep"]
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def canonical_domain(value: str) -> str:
    """Normalize one hostname to lower-case IDNA ASCII and validate labels."""
    candidate = value.strip().rstrip(".")
    try:
        domain = candidate.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("registered_domain must be a valid hostname") from exc
    labels = domain.split(".")
    if (
        not domain
        or len(domain) > 253
        or len(labels) < 2
        or any(not _HOST_LABEL.fullmatch(label) for label in labels)
    ):
        raise ValueError("registered_domain must be a valid hostname")
    return domain


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


Reason = Annotated[str, Field(json_schema_extra={"minLength": 1})]


class OpsSignInRequest(_Request):
    email: EmailStr
    password: Annotated[str, Field(min_length=1, max_length=MAX_LENGTH)]
    totp_code: Annotated[str, Field(pattern=r"^[0-9]{6}$")]


class OpsSignInView(BaseModel):
    ops_account_id: UUID
    display_name: str
    session_expires_at: datetime


class OpsOrgCreate(_Request):
    name: Annotated[str, Field(min_length=1)]
    registered_domain: str
    timezone: str = "Africa/Cairo"
    service_start_on: date | None = None
    service_last_access_on: date | None = None
    contract_reference: str | None = None
    retention_policy: OrgRetentionPeriodInput | None = None
    reason: Reason

    @model_validator(mode="after")
    def _term_bundle(self) -> OpsOrgCreate:
        supplied = (
            self.service_start_on is not None,
            self.service_last_access_on is not None,
            self.contract_reference is not None,
        )
        if any(supplied) and not all(supplied):
            raise ValueError("service dates and contract reference must be supplied together")
        if self.retention_policy is not None and not all(supplied):
            raise ValueError("retention policy requires a confirmed service term")
        return self

    @field_validator("registered_domain")
    @classmethod
    def _domain(cls, value: str) -> str:
        return canonical_domain(value)

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("timezone must be an IANA zone") from exc
        return value


class OpsOrgView(BaseModel):
    org_id: UUID
    name: str
    registered_domain: str
    timezone: str
    service_starts_at: datetime | None = None
    service_ends_at: datetime | None = None
    projected_purge_eligible_at: datetime | None = None


RetentionUnit = Literal["elapsed_days", "calendar_days", "calendar_months"]


class ServiceTermPreviewRequest(_Request):
    timezone: str
    service_start_on: date
    service_last_access_on: date
    period_value: Annotated[int, Field(ge=1)]
    period_unit: RetentionUnit

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, value: str) -> str:
        return OpsOrgCreate._timezone(value)

    @model_validator(mode="after")
    def _dates(self) -> ServiceTermPreviewRequest:
        if self.service_last_access_on < self.service_start_on:
            raise ValueError("service_last_access_on must not precede service_start_on")
        return self


class ServiceTermPreviewView(BaseModel):
    service_starts_at: datetime
    service_ends_at: datetime
    projected_purge_eligible_at: datetime


class OrgRetentionPeriodInput(_Request):
    policy_reference: Annotated[str, Field(min_length=1)]
    period_value: Annotated[int, Field(ge=1)]
    period_unit: RetentionUnit
    calendar_timezone: str | None = None

    @model_validator(mode="after")
    def _calendar_zone(self) -> OrgRetentionPeriodInput:
        if self.period_unit == "elapsed_days" and self.calendar_timezone is not None:
            raise ValueError("elapsed-day retention has no calendar timezone")
        if self.period_unit != "elapsed_days":
            if self.calendar_timezone is None:
                raise ValueError("calendar retention requires a timezone")
            OpsOrgCreate._timezone(self.calendar_timezone)
        return self


OpsOrgCreate.model_rebuild()


class OrgServiceTermCommand(_Request):
    expected_sequence: Annotated[int, Field(ge=0)]
    current_offboarding_id: UUID | None = None
    service_start_on: date
    service_last_access_on: date
    contract_reference: Annotated[str, Field(min_length=1)]
    retention_policy: OrgRetentionPeriodInput | None = None
    reason: Annotated[str, Field(min_length=1)]

    @model_validator(mode="after")
    def _dates(self) -> OrgServiceTermCommand:
        if self.service_last_access_on < self.service_start_on:
            raise ValueError("service_last_access_on must not precede service_start_on")
        return self


class OrgLifecycleView(BaseModel):
    org_id: UUID
    lifecycle_status: Literal["active", "offboarding", "purging"]
    access_status: Literal["unconfigured", "scheduled", "available", "hold", "purging"]
    lifecycle_sequence: int
    history_verified: bool
    service_term_enforced: bool
    service_term_id: UUID | None = None
    service_start_on: date | None = None
    service_last_access_on: date | None = None
    service_starts_at: datetime | None = None
    service_ends_at: datetime | None = None
    projected_purge_eligible_at: datetime | None = None
    service_timezone: str | None = None
    contract_reference: str | None = None
    pending_operation_id: UUID | None = None
    pending_operation_status: Literal["pending", "applied", "rejected"] | None = None
    offboarding_id: UUID | None = None
    offboarding_started_at: datetime | None = None
    offboarding_started_by: UUID | None = None
    purge_eligible_at: datetime | None = None
    retention_policy_reference: str | None = None
    restrictions: list[dict[str, Any]] = Field(default_factory=list)


class OrgRetentionPolicyView(BaseModel):
    org_id: UUID
    source: Literal["contract", "default"]
    policy_reference: str
    contract_reference: str | None = None
    period_value: int
    period_unit: RetentionUnit
    calendar_timezone: str | None = None
    lifecycle_sequence: int
    history_verified: bool
    approved_at: datetime | None = None


class OpsAccountCreate(_Request):
    org_id: UUID
    email: EmailStr
    display_name: Annotated[str, Field(min_length=1)]
    role: Role
    manager_account_id: UUID | None = None
    reason: Reason

    @model_validator(mode="after")
    def _manager_shape(self) -> OpsAccountCreate:
        if self.role == "rep" and self.manager_account_id is None:
            raise ValueError("manager_account_id is required for role=rep")
        if self.role == "manager" and self.manager_account_id is not None:
            raise ValueError("manager_account_id is not allowed for role=manager")
        return self


class OpsAccountView(BaseModel):
    account_id: UUID
    email: EmailStr
    role: Role


class OpsReasonBody(_Request):
    reason: Reason


class TeamChangeRequest(OpsReasonBody):
    new_manager_account_id: UUID


class PositionTransferRequest(OpsReasonBody):
    new_manager_account_id: UUID


SubjectKind = Literal["account", "candidate"]


class SubjectRequestCreate(OpsReasonBody):
    org_id: UUID
    subject_kind: SubjectKind
    subject_id: UUID


class ErasureRequestView(BaseModel):
    id: UUID
    org_id: UUID
    subject_kind: SubjectKind
    subject_id: UUID
    status: Literal["pending", "processing", "awaiting_input", "executed", "failed", "rejected", "withdrawn"]
    request_policy_reference: str
    response_due_at: datetime | None = None
    restriction_id: UUID | None = None
    requested_at: datetime
    executed_at: datetime | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)


class ExportRequestView(BaseModel):
    id: UUID
    org_id: UUID
    subject_kind: SubjectKind
    subject_id: UUID
    status: Literal["pending", "processing", "awaiting_input", "ready", "delivered", "failed", "rejected", "withdrawn"]
    request_policy_reference: str
    response_due_at: datetime | None = None
    restriction_id: UUID | None = None
    requested_at: datetime
    ready_at: datetime | None = None
    expires_at: datetime | None = None
    bundle_url: str | None = None


class AuditEntry(BaseModel):
    id: UUID
    ops_account_id: UUID
    verb: Literal[
        "provision_org",
        "provision_account",
        "deactivate_account",
        "change_team_mapping",
        "transfer_position",
        "resolve_fault",
        "execute_erasure",
        "execute_export",
    ]
    target_org_id: UUID | None = None
    target_ref: dict[str, Any]
    reason: str
    occurred_at: datetime


class CursorPage(BaseModel):
    next_cursor: str | None = None
    has_more: bool


class ErasureRequestPage(BaseModel):
    data: list[ErasureRequestView]
    pagination: CursorPage


class ExportRequestPage(BaseModel):
    data: list[ExportRequestView]
    pagination: CursorPage


class AuditPage(BaseModel):
    data: list[AuditEntry]
    pagination: CursorPage


class ResolveFaultRequest(_Request):
    reason: Annotated[str, Field(min_length=1, max_length=1_000)]


class FaultView(BaseModel):
    id: UUID
    org_id: UUID
    kind: Literal["grading_failure", "playback_asset"]
    attempt_id: UUID
    status: Literal["open", "resolved"]
    detail: dict[str, Any]
    opened_at: datetime
    resolved_at: datetime | None = None
    resolution_note: str | None = None


class FaultPage(BaseModel):
    data: list[FaultView]
    pagination: CursorPage
