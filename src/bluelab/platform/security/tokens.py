"""Capability and reset tokens.

Candidate tokens are 256-bit `secrets.token_urlsafe` values, stored hashed,
carrying the `(org, position, candidate)` binding — and are validated *against
the binding*, never merely for existence (architecture/02 §3.2, FR-IDA-011).
Reset tokens are time-limited, single-use, hashed (FR-IDA-006).

## "Validated against the binding, never merely for existence"

This is the sentence the whole module exists to make true, and it is easy to
implement wrong. The wrong version:

    token_row = await lookup(token_hash)
    if token_row: admit()                      # ← WRONG

That admits any valid token to any resource the handler goes on to name. The
right version resolves the token *into* a scope tuple and lets every subsequent
query be bounded by it — the candidate's own position, their own stages, their
own attempts. `CandidateBinding` is that tuple, and `verify_candidate_token`
returns it rather than a boolean, so there is nothing to accidentally ignore.

## Why SHA-256 here and argon2id for passwords

Not an inconsistency. A password is low-entropy and human-chosen, so a stolen
hash must be expensive to attack — hence memory-hard argon2id. A token is 256
bits of CSPRNG output; brute force is already impossible, so the only job of the
hash is to make a database leak useless, and SHA-256 does that. Using argon2id
would add tens of milliseconds to every candidate request for no security gain.

## The fragment, and why it never reaches the server's logs

The invite link carries the token in the **URL fragment**, which browsers do not
send. The SPA extracts it client-side and sends it as a bearer header — so it
never appears in a path, a query string, an access log, or a `Referer`
(api/00 §3).
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final
from uuid import UUID

from bluelab.platform.clock import now

TOKEN_BYTES: Final = 32
"""256 bits (FR-IDA-011)."""

RESET_TOKEN_TTL: Final = timedelta(hours=1)
"""Reset links are time-limited (FR-IDA-006). One hour is long enough to survive
mail-delivery delay and short enough that a forwarded mailbox is not a standing
account takeover."""


def mint_token() -> str:
    """A fresh 256-bit URL-safe token. The only place tokens are created."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    """The stored form. The plaintext is never persisted, only ever emailed."""
    return hashlib.sha256(token.encode()).hexdigest()


def tokens_equal(candidate: str, expected: str) -> bool:
    """Constant-time comparison of two token hashes.

    `==` on strings short-circuits at the first differing byte, which leaks a
    prefix-match oracle. Irrelevant against 256-bit entropy in practice, used
    anyway because the habit is what protects the case where it does matter.
    """
    return secrets.compare_digest(candidate, expected)


@dataclass(frozen=True, slots=True)
class CandidateBinding:
    """What a candidate token resolves to — the scope, not a yes/no.

    Returned instead of a boolean so a caller cannot admit a valid token and then
    operate on a resource it was not bound to.
    """

    org_id: UUID
    team_id: UUID
    position_id: UUID
    candidate_id: UUID
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class StoredCandidateToken:
    """The persisted token row, as the identity module stores it."""

    token_hash: str
    org_id: UUID
    team_id: UUID
    position_id: UUID
    candidate_id: UUID
    expires_at: datetime
    revoked: bool = False


class TokenOutcome(StrEnum):
    """The **only** two outcomes a client may observe (ux/05 §3.3).

    `token-expired` is separated from `token-invalid` deliberately, because an
    expired link needs different advice from a wrong one — and expiry is already
    disclosed to the candidate by `meta.expired_at`.

    Everything else collapses to `INVALID`. "Revoked" and "unknown" must be
    indistinguishable: telling a caller a token was *revoked* confirms it once
    existed, which turns the endpoint into a candidate-enumeration oracle — the
    same leak `platform.errors.denial` closes for resources.
    """

    INVALID = "token-invalid"
    EXPIRED = "token-expired"


class TokenInvalid(Exception):
    """The token is unusable.

    `reason` is diagnostic and **log-only**. `outcome` is what may reach a
    client. They are separate attributes rather than one so that surfacing the
    wrong one has to be a deliberate act rather than a slip.
    """

    def __init__(self, reason: str, *, outcome: TokenOutcome = TokenOutcome.INVALID) -> None:
        super().__init__(reason)
        self.reason = reason
        self.outcome = outcome


def verify_candidate_token(
    presented: str,
    stored: StoredCandidateToken | None,
    *,
    at: datetime | None = None,
) -> CandidateBinding:
    """Resolve a presented token into its binding.

    Args:
        presented: The raw token from the `Authorization: Bearer` header.
        stored: The row looked up by `hash_token(presented)`, or None.
        at: Evaluation time; defaults to now.

    Returns:
        The `(org, team, position, candidate)` binding to scope every subsequent
        query with.

    Raises:
        TokenInvalid: Unknown, revoked, or expired.
    """
    if stored is None:
        raise TokenInvalid("unknown")
    if not tokens_equal(hash_token(presented), stored.token_hash):
        raise TokenInvalid("mismatch")
    if stored.revoked:
        raise TokenInvalid("revoked")

    instant = at or now()
    if stored.expires_at <= instant:
        raise TokenInvalid("expired", outcome=TokenOutcome.EXPIRED)

    return CandidateBinding(
        org_id=stored.org_id,
        team_id=stored.team_id,
        position_id=stored.position_id,
        candidate_id=stored.candidate_id,
        expires_at=stored.expires_at,
    )


@dataclass(frozen=True, slots=True)
class ResetToken:
    """A minted password-reset token.

    `plaintext` is emailed and then discarded; only `token_hash` is stored.
    Single-use: consuming it deletes the row, so a replay finds nothing rather
    than finding a used marker it might mis-handle.
    """

    plaintext: str
    token_hash: str
    expires_at: datetime


def mint_reset_token(*, at: datetime | None = None) -> ResetToken:
    """Mint a time-limited, single-use reset token (FR-IDA-006)."""
    plaintext = mint_token()
    return ResetToken(
        plaintext=plaintext,
        token_hash=hash_token(plaintext),
        expires_at=(at or now()) + RESET_TOKEN_TTL,
    )
