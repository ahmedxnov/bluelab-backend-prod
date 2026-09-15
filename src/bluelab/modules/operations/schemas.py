"""Pydantic request and response models for this module's slice of the contract.

The contract is `api/openapi.yaml` and it is authoritative (ADR-0035): these
models are checked against it by the conformance diff, they do not define it.
`snake_case` fields, singular per glossary (api/00 §2).
"""

from __future__ import annotations

import re
from datetime import datetime
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
    reason: Reason

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
