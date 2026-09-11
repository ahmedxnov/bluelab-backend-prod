"""RFC 6238 verification and single-step replay prevention for operations MFA.

Codes use the RFC defaults fixed by the security contract: HMAC-SHA-1, six
digits, and a 30-second step. Verification accepts the current step or one
adjacent step. A successful step is claimed atomically in Valkey, so concurrent
requests cannot turn one code into two sessions.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import struct
from datetime import datetime
from typing import Final
from uuid import UUID

from valkey.asyncio import Valkey

STEP_SECONDS: Final = 30
DIGITS: Final = 6
REPLAY_TTL_SECONDS: Final = 120
_REPLAY_PREFIX: Final = "ops:totp:accepted:"


def _key(secret: str) -> bytes:
    """Decode an RFC 4648 base32 seed without accepting malformed input."""
    normalized = secret.strip().replace(" ", "").upper()
    if not normalized:
        raise ValueError("a TOTP seed cannot be empty")
    padding = "=" * (-len(normalized) % 8)
    try:
        return base64.b32decode(normalized + padding, casefold=False)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("the TOTP seed is not valid base32") from exc


def code_for_step(secret: str, step: int) -> str:
    """Return the six-digit RFC 6238 code for one non-negative time step."""
    if step < 0:
        raise ValueError("a TOTP time step cannot be negative")
    digest = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    dynamic = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{dynamic % (10**DIGITS):0{DIGITS}d}"


def matching_step(secret: str, code: str, *, at: datetime) -> int | None:
    """Resolve a valid code to its time step, preferring the current step."""
    if len(code) != DIGITS or not code.isascii() or not code.isdigit():
        return None
    current = int(at.timestamp()) // STEP_SECONDS
    for step in (current, current - 1, current + 1):
        if step >= 0 and hmac.compare_digest(code_for_step(secret, step), code):
            return step
    return None


class TotpReplayStore:
    """Atomically remember an accepted operator/time-step pair."""

    def __init__(self, client: Valkey) -> None:
        self._client = client

    async def claim(self, *, ops_account_id: UUID, step: int) -> bool:
        """Return true exactly once for an operator and accepted step."""
        key = f"{_REPLAY_PREFIX}{ops_account_id}:{step}"
        claimed = await self._client.set(key, "1", ex=REPLAY_TTL_SECONDS, nx=True)
        return bool(claimed)
