"""Pydantic request and response models for this module's slice of the contract.

The contract is `api/openapi.yaml` and it is authoritative (ADR-0035): these
models are checked against it by the conformance diff, they do not define it.
`snake_case` fields, singular per glossary (api/00 §2).

## Why the request models are strict and the response model is not

The request models set `extra="forbid"`. An unknown member in a request body is a
client that believes it is sending something this server will honour, and
silently dropping it is how a control gets "enabled" client-side and ignored
server-side. api/04 §3 makes new *optional* request fields additive, so
forbidding unknown ones costs nothing a compatible client would do.

`SessionView` is the opposite direction: clients must ignore unknown response
members (api/04 §3, a generated-client obligation), which is what lets the server
add a field without a version bump.
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from bluelab.platform.security.passwords import MAX_LENGTH, MIN_LENGTH

Gate = Literal["first_sign_in", "consent", "terms"]
"""The three gates a session can be limited by (api/00 §3)."""


class _Request(BaseModel):
    """Strict by default — see the module docstring."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SignInRequest(_Request):
    email: EmailStr
    password: Annotated[str, Field(min_length=1, max_length=MAX_LENGTH)]
    """Bounded, but deliberately NOT floor-validated.

    `min_length=1` rather than the policy minimum: this is the field a *sign-in*
    carries. Rejecting a short password with `422 validation-error` while a
    policy-length wrong one gets `401 invalid-credentials` would tell an attacker
    which of their guesses are even policy-shaped, from the status code alone.
    The floor belongs on the fields that *set* a password, below.

    The ceiling stays, because argon2id is memory-hard: an unbounded input is a
    denial of service aimed at ourselves (`passwords.MAX_LENGTH`).
    """


class _NewPassword(_Request):
    """The shared shape of every password-*setting* field."""

    new_password: Annotated[str, Field(min_length=MIN_LENGTH, max_length=MAX_LENGTH)]


class FirstSignInRequest(_NewPassword):
    """All three actions on one screen; any subset alone does not grant access
    (FR-IDA-004)."""

    consent: bool
    """The recording-consent checkbox — its own distinct, never pre-selected
    control (CMP-002). `false` answers 422 and records nothing (AC-IDA-002)."""

    terms_accepted: bool
    """Terms of Use + Privacy Notice acknowledgment — a separate instrument from
    consent (CMP-005)."""


class AcceptancesRequest(_Request):
    """Supply only the pending instruments (CMP-002/CMP-005 re-request on a
    material document change)."""

    consent: bool | None = None
    terms_accepted: bool | None = None


class OrgView(BaseModel):
    id: UUID
    name: str
    timezone: str


class SessionView(BaseModel):
    """What `POST` and `GET /auth/session` both return.

    Carries no password state, no session id, and no credential material. A
    response body is the one place a session identifier must never appear: it is
    readable by any script that reaches the response, which defeats the entire
    point of an `HttpOnly` cookie.
    """

    account_id: UUID
    display_name: str
    email: EmailStr
    role: Literal["manager", "rep"]
    team_id: UUID | None = None
    org: OrgView
    pending_gates: list[Gate]
    """Gates blocking full access; empty when none (api/00 §3)."""
