"""argon2id password hashing at current OWASP parameters (`argon2-cffi`).

Chosen over bcrypt because argon2id is memory-hard: a GPU attacking bcrypt is
bounded by compute, which scales cheaply; against argon2id it is bounded by RAM
per guess, which does not.

## Two behaviours worth knowing

**`needs_rehash` is not optional.** Parameters get raised as hardware improves.
A hash minted under old parameters stays valid, so it must be silently upgraded
on the next successful sign-in — that is the only moment the plaintext exists.

**`dummy_verify` exists to flatten sign-in timing.** Verifying a real hash costs
tens of milliseconds; returning early for an unknown email costs none. That gap
is a user-enumeration oracle: an attacker learns which addresses have accounts by
timing the failure. So the sign-in path verifies against a fixed decoy hash when
the account does not exist, and both branches cost the same.

This is the *authentication* sibling of the denial-timing rule in
`platform.errors.denial` — same problem, different layer, and here it does need
an explicit equaliser because there is no RLS query doing the work.
"""

from __future__ import annotations

from typing import Final

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher: Final = PasswordHasher(
    # OWASP Password Storage Cheat Sheet, argon2id: 19 MiB, t=2, p=1.
    time_cost=2,
    memory_cost=19 * 1024,
    parallelism=1,
    hash_len=32,
    salt_len=16,
)

_DECOY_HASH: Final = _hasher.hash("decoy-for-constant-time-verification")
"""A real hash of a value nothing can match. Verified against when the account
does not exist, so the unknown-email path costs the same as the wrong-password
path."""

MIN_LENGTH: Final = 12
MAX_LENGTH: Final = 1024
"""Bounded so a multi-megabyte password cannot turn one request into a
memory-hard denial of service against ourselves."""


def hash_password(plaintext: str) -> str:
    """Hash a password for storage.

    Returns:
        The encoded hash, which carries its own parameters and salt.
    """
    return _hasher.hash(plaintext)


def verify_password(plaintext: str, encoded_hash: str) -> bool:
    """Check a password against a stored hash.

    Returns False rather than raising on mismatch: a wrong password is an
    expected outcome of a sign-in attempt, not an exceptional one.
    """
    try:
        return _hasher.verify(encoded_hash, plaintext)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def dummy_verify() -> None:
    """Burn the cost of a verification when no account exists.

    Call on the unknown-email branch of sign-in so both branches take the same
    time. Without it, response latency reveals which addresses are provisioned —
    and provisioning is operator-mediated, so the account list is a meaningful
    thing to learn about a customer org (SEC-005).
    """
    try:
        _hasher.verify(_DECOY_HASH, "wrong")
    except (VerifyMismatchError, VerificationError):
        pass


def needs_rehash(encoded_hash: str) -> bool:
    """True if the hash was minted under weaker parameters than current.

    Check on every successful sign-in and re-hash when true — that is the only
    point at which the plaintext is available to upgrade with.
    """
    try:
        return _hasher.check_needs_rehash(encoded_hash)
    except InvalidHashError:
        return True
