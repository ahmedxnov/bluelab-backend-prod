"""The `Idempotency-Key` replay store (api/00 §6, ADR-0040).

Used only where the domain has no natural key — candidate batch create, invite
send, shortlist send. Same key + same body replays the stored response; same key
+ different body is `409 idempotency-key-reuse`. Everywhere else the guard is
domain-natural (a lease, a version CAS, a status transition, a unique row).

## Why the store is a table and not Valkey

The coordination store is deliberately volatile — nothing there survives its call
(architecture/00 §3.4). A replay record must survive: the whole point is that a
client retrying after a timeout gets the original answer rather than sending a
second batch of candidate invites. So it lives in Postgres, and the record is
written **in the same transaction as the work it describes**. A record in a
separate store could commit while the work rolled back, which would turn a retry
into a silent no-op that never happened.

## Why the body is fingerprinted rather than stored

Storing the request body would put candidate names and email addresses into a
second place with its own retention question, for no benefit. A SHA-256 of the
canonical body answers the only question asked of it — *is this the same request*
— and carries no personal data. Erasure then has one fewer surface to reach
(CMP-001).

## Scoping

Keys are scoped to `(org, principal, endpoint)`. A key is a client-chosen string,
so an unscoped namespace would let one org's key collide with another's — and a
collision here replays *someone else's response*. That is a cross-tenant leak
wearing a caching bug's clothes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final
from uuid import UUID

from bluelab.platform.errors import catalog
from bluelab.platform.errors.denial import ProblemError

RETENTION: Final = timedelta(hours=24)
"""How long a replay record is honoured.

Long enough to cover any client retry — the SPA's fetch layer gives up in
seconds — and short enough that the table stays small. Beyond the window the key
is simply unknown, and the request executes again.
"""

MAX_KEY_LENGTH: Final = 255


@dataclass(frozen=True, slots=True)
class IdempotencyKey:
    """A validated, scoped key."""

    value: str
    org_id: UUID
    principal_id: UUID
    endpoint: str

    @property
    def storage_key(self) -> str:
        """The scoped key, so one org's key cannot collide with another's."""
        return f"{self.org_id}:{self.principal_id}:{self.endpoint}:{self.value}"


@dataclass(frozen=True, slots=True)
class StoredResponse:
    """A previously returned response, replayed verbatim on a matching retry."""

    status: int
    body: dict[str, Any]
    fingerprint: str


def require_key(
    raw: str | None,
    *,
    org_id: UUID,
    principal_id: UUID,
    endpoint: str,
) -> IdempotencyKey:
    """Validate a required `Idempotency-Key` header.

    Required on exactly three operations — candidate batch create, invite send,
    shortlist send — because those are batch sends with no natural request key
    (api/00 §6).

    Raises:
        ProblemError: `422 idempotency-key-required` if absent or unusable.
    """
    if raw is None or not raw.strip():
        raise ProblemError(catalog.IDEMPOTENCY_KEY_REQUIRED)
    key = raw.strip()
    if len(key) > MAX_KEY_LENGTH:
        raise ProblemError(
            catalog.IDEMPOTENCY_KEY_REQUIRED,
            detail=f"key exceeds {MAX_KEY_LENGTH} characters",
        )
    return IdempotencyKey(
        value=key, org_id=org_id, principal_id=principal_id, endpoint=endpoint
    )


def fingerprint(body: Any) -> str:
    """SHA-256 over the canonical JSON form of a request body.

    Canonical — sorted keys, no insignificant whitespace — so a semantically
    identical retry that serialised its JSON differently still matches. Without
    that, a client changing library versions would look like a different request
    and get a spurious `409`.
    """
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def check_replay(stored: StoredResponse | None, *, body: Any) -> StoredResponse | None:
    """Decide what a repeated key means.

    Returns:
        The stored response to replay, or None if this key has not been seen and
        the request should execute.

    Raises:
        ProblemError: `409 idempotency-key-reuse` when the same key arrives with
            a *different* body. Executing would be worse than refusing: the
            client believes it is retrying, and the server would be performing a
            second, different action — a second batch of invites to a different
            list of people.
    """
    if stored is None:
        return None
    if stored.fingerprint != fingerprint(body):
        raise ProblemError(catalog.IDEMPOTENCY_KEY_REUSE)
    return stored
