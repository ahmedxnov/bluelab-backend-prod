"""Server-side opaque sessions in Valkey (ADR-0024, ADR-0028).

Chosen over JWTs precisely for revocation latency: deactivation must revoke
instantly (FR-IDA-010). Idle and absolute lifetimes are policy values owned by
security/04 §3, not constants invented here.

## Why not JWTs, concretely

A JWT is valid until it expires. Deactivating an account would leave every issued
token working for the rest of its lifetime, and the only fixes are a revocation
list — which is a server-side session with extra steps — or very short
expiries, which trade the problem for refresh churn. FR-IDA-010 says BlueLab ops
deactivates an account when someone leaves; "leaves in fifteen minutes" is not
what that means.

## Two clocks, not one

* **Idle** (default 12 h) slides on each use. It ends sessions people walked away
  from.
* **Absolute** (default 7 d) does not slide. It bounds how long a stolen cookie
  is worth stealing, no matter how actively it is used.

Both are stored inside the record, and the Valkey TTL is set to the *nearer* of
the two so an expired session cannot linger as a key.

## Revocation is a fan-out, so the index is not optional

Deactivation must kill every session an account holds, across devices. A
`SET` per account indexes them; without it, revocation would need a keyspace
scan — which on a shared Valkey is both slow and the kind of operation that gets
disabled in production.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID

from valkey.asyncio import Valkey

from bluelab.platform.clock import now
from bluelab.platform.security.tokens import hash_token, mint_token

_SESSION_PREFIX: Final = "session:"
_ACCOUNT_INDEX_PREFIX: Final = "session:account:"


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """What a session cookie resolves to.

    Deliberately small: identifiers and the two clocks. No display name, no
    permissions snapshot — a stale copy of either would be a correctness bug the
    moment ops changed a rep's team (FR-IDA-009).
    """

    account_id: str
    org_id: str
    team_id: str
    role: str
    created_at: str
    last_seen_at: str
    absolute_expires_at: str

    gate: str | None = None
    """The pending gate, if any: `first_sign_in`, `consent`, or `terms`.

    A gate-limited session is a real session that answers `409` on everything
    except the gate endpoints (api/00 §3). Carrying the gate here means the
    limitation cannot be bypassed by hitting a route that forgot to check.
    """


def _decode(payload: str | bytes) -> SessionRecord:
    """Decode a stored record, tolerating fields this release does not know.

    The same expand-only discipline the queue envelope follows (`queue/compat`),
    and for the same reason: during a rolling deploy, release N+1 writes a record
    that release N reads. A strict `SessionRecord(**json.loads(...))` raises
    `TypeError` on an unknown key, which would turn every request from that user
    into a 500 for the length of the deploy.

    Unknown keys are dropped. A *missing* required key is still an error — that
    is a genuinely unreadable record rather than a newer one.
    """
    raw = json.loads(payload)
    known = set(SessionRecord.__dataclass_fields__)
    return SessionRecord(**{key: value for key, value in raw.items() if key in known})


class SessionStore:
    """Opaque server-side sessions.

    The cookie carries a random 256-bit id; Valkey holds the record keyed by its
    **hash**, so a dump of the coordination store does not yield usable session
    cookies.
    """

    def __init__(self, client: Valkey, *, idle_seconds: int, absolute_seconds: int) -> None:
        self._client = client
        self._idle = timedelta(seconds=idle_seconds)
        self._absolute = timedelta(seconds=absolute_seconds)

    async def create(
        self,
        *,
        account_id: UUID,
        org_id: UUID,
        team_id: UUID,
        role: str,
        gate: str | None = None,
    ) -> str:
        """Open a session and return the raw id for the cookie.

        The raw id is returned once and never stored; only its hash is persisted.
        """
        raw = mint_token()
        instant = now()
        record = SessionRecord(
            account_id=str(account_id),
            org_id=str(org_id),
            team_id=str(team_id),
            role=role,
            created_at=instant.isoformat(),
            last_seen_at=instant.isoformat(),
            absolute_expires_at=(instant + self._absolute).isoformat(),
            gate=gate,
        )

        key = _SESSION_PREFIX + hash_token(raw)
        async with self._client.pipeline(transaction=True) as pipe:
            pipe.set(key, json.dumps(asdict(record)), ex=self._ttl_seconds(record, instant))
            pipe.sadd(_ACCOUNT_INDEX_PREFIX + str(account_id), key)
            pipe.expire(_ACCOUNT_INDEX_PREFIX + str(account_id), int(self._absolute.total_seconds()))
            await pipe.execute()
        return raw

    async def resolve(self, raw: str) -> SessionRecord | None:
        """Look up a session and slide its idle clock.

        Returns None for unknown, expired, or revoked — the caller answers
        `401 session-invalid` for all three without distinguishing them.
        """
        key = _SESSION_PREFIX + hash_token(raw)
        payload = await self._client.get(key)
        if payload is None:
            return None

        record = _decode(payload)
        instant = now()

        if datetime.fromisoformat(record.absolute_expires_at) <= instant:
            await self._client.delete(key)
            return None

        slid = replace(record, last_seen_at=instant.isoformat())
        await self._client.set(key, json.dumps(asdict(slid)), ex=self._ttl_seconds(slid, instant))
        return slid

    async def set_gate_and_rotate(self, raw: str, *, gate: str | None) -> str | None:
        """Move the session to `gate` — usually None — and **issue a new id**.

        Args:
            raw: The current raw session id.
            gate: The gate that still limits this session, or None when none does.

        Returns:
            The new raw session id for the cookie, or None if the session had
            already expired.

        This took no `gate` argument and always cleared to None. That is correct
        for first sign-in, where completing the gate clears every instrument at
        once, and **wrong for acceptances**: an account behind BOTH consent and
        terms that supplies only consent still owes terms. Clearing
        unconditionally would have handed it the full product surface with a
        compliance instrument outstanding. The caller recomputes what remains and
        passes it.

        The rotation is the security-relevant half. Clearing the first-sign-in
        gate is the moment the user sets their real password — an authentication
        level change — and ASVS 3.2.1 / OWASP A07 require a new session
        identifier at that point. Keeping the id would leave any value captured
        before the change (a shoulder-surfed cookie, a shared machine, a proxy
        log from the provisioning email flow) valid against the now
        fully-privileged session.

        Implemented as create-new-then-delete-old so a failure between the two
        leaves the user with a working session rather than locked out.
        """
        old_key = _SESSION_PREFIX + hash_token(raw)
        payload = await self._client.get(old_key)
        if payload is None:
            return None

        record = replace(_decode(payload), gate=gate)
        new_raw = mint_token()
        new_key = _SESSION_PREFIX + hash_token(new_raw)
        index = _ACCOUNT_INDEX_PREFIX + record.account_id

        async with self._client.pipeline(transaction=True) as pipe:
            pipe.set(new_key, json.dumps(asdict(record)), ex=self._ttl_seconds(record, now()))
            pipe.sadd(index, new_key)
            pipe.srem(index, old_key)
            pipe.delete(old_key)
            await pipe.execute()

        return new_raw

    async def revoke(self, raw: str) -> None:
        """End one session — sign-out."""
        await self._client.delete(_SESSION_PREFIX + hash_token(raw))

    async def revoke_all(self, account_id: UUID) -> int:
        """End every session an account holds, immediately.

        This is FR-IDA-010's teeth. Called on deactivation, on a team change that
        alters scope, and on password reset.

        Returns:
            How many sessions were ended.
        """
        index = _ACCOUNT_INDEX_PREFIX + str(account_id)
        # `smembers` is typed `Awaitable[set] | set` because valkey-py shares one
        # signature between its sync and async clients. This one is async, so the
        # await is correct and the union is a stub artefact.
        keys: set[str] = await self._client.smembers(index)  # type: ignore[misc]
        if not keys:
            return 0
        async with self._client.pipeline(transaction=True) as pipe:
            pipe.delete(*keys)
            pipe.delete(index)
            await pipe.execute()
        return len(keys)

    def _ttl_seconds(self, record: SessionRecord, instant: datetime) -> int:
        """The nearer of the two clocks, so no key outlives its session."""
        absolute_remaining = datetime.fromisoformat(record.absolute_expires_at) - instant
        return max(1, int(min(self._idle, absolute_remaining).total_seconds()))
