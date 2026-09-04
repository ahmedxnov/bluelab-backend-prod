"""Fixed-window attempt throttling for the authentication surface (SEC-004/005).

## Why this exists at all, given argon2id

The password hash is deliberately expensive — `memory_cost` is 19 MiB per
verification (RFC 9106's second recommended profile). That cost is what makes an
offline attack on a stolen hash impractical. Online, it points the other way: every
sign-in request the server accepts allocates 19 MiB and burns CPU before it can
answer, so an unauthenticated caller can spend our memory at whatever rate they
choose.

`identity.service.authenticate` makes that worse on purpose. An unknown email still
runs `dummy_verify()`, because returning early would answer in microseconds and
turn response time into a user-enumeration oracle. The timing defence is correct
and it means **an attacker needs no valid address** to make us pay full price.

`passwords.MAX_LENGTH` already bounds how expensive one attempt can be. This bounds
how many attempts there are. Both halves are needed; only the first existed.

## Two counters, because they stop different attacks

**Per identifier** — repeated guessing against one account. Small limit.

**Per source** — spraying one guess across many addresses, which slips past a
per-identifier counter entirely and is also the shape the memory-exhaustion attack
takes. Larger limit, same window.

An attempt counts against both. Either tripping refuses the request *before* the
hash runs, which is the only placement that saves the cost.

## The identifier is hashed before it becomes a key

Valkey holds `throttle:identifier:<sha256(email)>`, never the address itself.
Whether an email is provisioned is exactly what sign-in may not disclose
(FR-IDA-005), and a coordination-store dump that listed every address anyone had
tried to sign in as would hand over a good part of the customer's account list.

## Fixed window, and the TTL is set once

`EXPIRE ... NX` on first increment only. Refreshing the TTL on every hit would let
a persistent attacker hold a legitimate user's counter open indefinitely — the
window would restart with each of the attacker's own attempts, so the victim's
lockout would never expire.

A fixed window admits the usual burst at a boundary: 2×limit across two adjacent
windows. That is accepted here. The attack this must stop is sustained, and the
alternative — a sliding log — stores one entry per attempt, which is a second
memory-exhaustion surface reached by the same unauthenticated request.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from valkey.asyncio import Valkey

from bluelab.platform.security.tokens import hash_token

_PREFIX: Final = "throttle:"


@dataclass(frozen=True, slots=True)
class Limit:
    """How many attempts are allowed, over how many seconds."""

    attempts: int
    window_seconds: int


class Throttle:
    """Attempt counters over the coordination store (C-13, ADR-0024)."""

    def __init__(self, client: Valkey) -> None:
        self._client = client

    @staticmethod
    def _key(bucket: str, subject: str) -> str:
        return f"{_PREFIX}{bucket}:{hash_token(subject)}"

    async def hit(self, bucket: str, subject: str, limit: Limit) -> int | None:
        """Count one attempt.

        Args:
            bucket: Which counter — `identifier` or `source`.
            subject: The value being counted. Hashed before it becomes a key.
            limit: Attempts allowed per window.

        Returns:
            None when the attempt is within the limit. Otherwise the number of
            seconds until the window resets, for `Retry-After`.

        Raises no exception of its own: a Valkey failure propagates. Sign-in
        cannot complete without Valkey anyway — `SessionStore.create` writes there
        — so there is no state in which failing open would let a real sign-in
        through. Swallowing the error would only remove the limiter at exactly the
        moment the store is under load.
        """
        key = self._key(bucket, subject)
        async with self._client.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            # NX: only when the key has no TTL yet, i.e. on the first attempt of a
            # window. Without it every hit would push the expiry out — see the
            # module docstring.
            pipe.expire(key, limit.window_seconds, nx=True)
            counted, _ = await pipe.execute()

        if int(counted) <= limit.attempts:
            return None

        remaining = await self._client.ttl(key)
        # A key with no TTL (-1) or already gone (-2) would otherwise report a
        # negative `Retry-After`. Fall back to the full window.
        return int(remaining) if remaining and int(remaining) > 0 else limit.window_seconds

    async def clear(self, bucket: str, subject: str) -> None:
        """Forget a subject's attempts.

        Called on a successful sign-in, so the counter measures *failures in a row*
        rather than lifetime activity. Without it, someone who mistypes their
        password a few times and then gets in stays near the limit for the rest of
        the window, and a shared office address would trip the source counter
        during an ordinary Monday morning.
        """
        await self._client.delete(self._key(bucket, subject))
