"""`X-Agent-Signature` — HMAC-SHA256 binding method, path, timestamp, and body,
verified constant-time, failing closed (ADR-0071 decision 2; api/02 §1.2).

This is the *only* shared secret between the application plane and the call
plane. It must stay version-locked with the identical primitive in
`Implementation/bluelab-agent-prod/src/bluelab_voice/signing.py`.

────────────────────────────────────────────────────────────────────────────────
ROUTE-BACK — amends api/02 §1.2 and ADR-0071 decision 2. Needs owner sign-off.
────────────────────────────────────────────────────────────────────────────────

Both documents specify the signature as *"HMAC-SHA256 over the exact raw body"*.
A security pass found that under-specifies the primitive to the point of being
exploitable:

  * `GET /internal/calls/{call_id}/bundle` is specified with an **empty body**.
    `HMAC(secret, b"")` is a **constant** — the same signature for every bundle
    request, for every call, forever. One observed header (a proxy log, a crash
    dump, an error report) is a permanent credential.
  * Nothing binds a signature to the **path**, so a captured signature is valid
    for any `call_id` — including other tenants'.
  * Nothing binds it to a **time**, so nothing expires.

The blast radius is bounded by the allowlist: the bundle structurally cannot
carry the answer key, the rubric, or any element of `drill_concealed`
(ADR-0071 rule 5). What an attacker gets is frozen scenario and participant-safe
persona content for arbitrary calls — the isolation design holding, but still an
unauthenticated cross-tenant read.

**The amendment:** sign a canonical string, not the bare body.

    METHOD \\n PATH \\n TIMESTAMP \\n sha256(body).hexdigest()

Every element earns its place: METHOD and PATH bind the signature to *this*
request; TIMESTAMP bounds replay; the body digest keeps the property the original
specification wanted, since a matching digest means the body is unaltered.

The agent must change to match. It has to change regardless — its endpoint paths
and its per-segment transcript streaming already diverge from the contract
(docs/call-plane-seam.md) — so this lands in the same pass at no extra cost.

## What is still not defended, deliberately

Replay **inside the skew window**, for the two POSTs. The guards there are
domain-natural rather than cryptographic: T-2 is keyed on the call, and
grade-once is enforced by `scorecard.attempt_id` unique (T-3), so a replayed
completion is a no-op rather than a second grading. A nonce store would duplicate
a guarantee the schema already gives, and would put state on the admission path.

## "Over the exact raw body" still holds

The digest must be computed over the **bytes as received**, before parsing and
before any re-serialisation. Parsing to JSON and re-encoding produces different
bytes — different key order, different whitespace, different unicode escaping.
In FastAPI that means reading `await request.body()` and digesting *that*, then
parsing. A handler that takes a Pydantic model parameter has already lost them.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Final

SIGNATURE_HEADER: Final = "X-Agent-Signature"
TIMESTAMP_HEADER: Final = "X-Agent-Timestamp"
"""Unix seconds, as a decimal string. Part of the signed material."""

MAX_SKEW_SECONDS: Final = 300
"""Accepted clock skew either side of now.

Five minutes is the usual webhook figure. Tighter would start rejecting
legitimate requests from a drifting call-plane host; looser would widen the
replay window for no benefit.
"""

_DIGEST_HEX_LENGTH: Final = 64  # SHA-256, hex-encoded


def canonical_string(*, method: str, path: str, timestamp: int, raw_body: bytes) -> str:
    """Build the exact string that gets signed.

    Args:
        method: HTTP method, upper-case (`GET`, `POST`).
        path: Request path **including** the `call_id`, without the query string
            — e.g. `/internal/calls/0197.../completion`. This is what binds a
            signature to one call.
        timestamp: Unix seconds, as sent in `X-Agent-Timestamp`.
        raw_body: The body exactly as received. Empty for `GET`.
    """
    body_digest = hashlib.sha256(raw_body).hexdigest()
    return f"{method.upper()}\n{path}\n{timestamp}\n{body_digest}"


def sign(secret: str, *, method: str, path: str, timestamp: int, raw_body: bytes) -> str:
    """Produce the signature for a request.

    Used by tests and by the agent's mirrored implementation, which must produce
    byte-identical output.
    """
    material = canonical_string(
        method=method, path=path, timestamp=timestamp, raw_body=raw_body
    )
    return hmac.new(secret.encode(), material.encode(), hashlib.sha256).hexdigest()


def verify(
    secret: str,
    *,
    method: str,
    path: str,
    raw_body: bytes,
    presented: str | None,
    timestamp_header: str | None,
    now_epoch: int,
) -> bool:
    """Verify a presented signature. Fails closed on every path.

    Args:
        secret: The shared HMAC secret.
        method: The request method.
        path: The request path, including the `call_id`.
        raw_body: The body **exactly as received** — not re-serialised.
        presented: The `X-Agent-Signature` header, or None.
        timestamp_header: The `X-Agent-Timestamp` header, or None.
        now_epoch: Current Unix seconds, injected so the skew check is testable.

    Returns:
        True only if the signature is present, well-formed, inside the skew
        window, and matches. Every other outcome is False — there is no branch
        that admits a request because verification could not be performed. This
        surface runs T-2 and T-6, so an admitted forgery writes to the
        transactional store.
    """
    if not presented or not timestamp_header:
        return False

    candidate = presented.strip().lower()
    if len(candidate) != _DIGEST_HEX_LENGTH:
        return False

    try:
        timestamp = int(timestamp_header.strip())
    except ValueError:
        return False

    if abs(now_epoch - timestamp) > MAX_SKEW_SECONDS:
        return False

    expected = sign(
        secret, method=method, path=path, timestamp=timestamp, raw_body=raw_body
    )
    return hmac.compare_digest(candidate, expected)
