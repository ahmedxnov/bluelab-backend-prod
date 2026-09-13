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
_SESSION_REVOCATION_PREFIX: Final = "session:revoked:"
_ACCOUNT_INDEX_PREFIX: Final = "session:account:"
_ACCOUNT_REVOCATION_PREFIX: Final = "session:account:revocation:"
_OPS_SESSION_PREFIX: Final = "ops:session:"
_OPS_SESSION_REVOCATION_PREFIX: Final = "ops:session:revoked:"
_OPS_ACCOUNT_INDEX_PREFIX: Final = "ops:session:account:"
_OPS_ACCOUNT_REVOCATION_PREFIX: Final = "ops:session:account:revocation:"


class SessionRevokedDuringCreation(RuntimeError):
    """The account was revoked after authentication but before session issue."""


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
    revocation_epoch: int = 0

    gate: str | None = None
    """The pending gate, if any: `first_sign_in`, `consent`, or `terms`.

    A compatibility snapshot, not an authorization decision. Request resolution
    reloads current account membership and legal gates before any product route.
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
        opened_at: datetime | None = None,
        revocation_epoch: int | None = None,
    ) -> str:
        """Open a session and return the raw id for the cookie.

        The raw id is returned once and never stored; only its hash is persisted.
        """
        raw = mint_token()
        instant = opened_at or now()
        epoch = (
            await self.revocation_epoch(account_id)
            if revocation_epoch is None
            else revocation_epoch
        )
        record = SessionRecord(
            account_id=str(account_id),
            org_id=str(org_id),
            team_id=str(team_id),
            role=role,
            created_at=instant.isoformat(),
            last_seen_at=instant.isoformat(),
            absolute_expires_at=(instant + self._absolute).isoformat(),
            revocation_epoch=epoch,
            gate=gate,
        )

        key = _SESSION_PREFIX + hash_token(raw)
        index = _ACCOUNT_INDEX_PREFIX + str(account_id)
        epoch_key = _ACCOUNT_REVOCATION_PREFIX + str(account_id)
        async with self._client.pipeline(transaction=True) as pipe:
            pipe.set(key, json.dumps(asdict(record)), ex=self._ttl_seconds(record, instant))
            pipe.sadd(index, key)
            pipe.expire(index, int(self._absolute.total_seconds()))
            pipe.expire(epoch_key, int(self._absolute.total_seconds()))
            await pipe.execute()
        if await self.revocation_epoch(account_id) != epoch:
            await self._remove_record(key, record)
            raise SessionRevokedDuringCreation("account sessions were revoked during issue")
        return raw

    async def revocation_epoch(self, account_id: UUID) -> int:
        """Return the account generation captured by a prospective session."""
        value = await self._client.get(_ACCOUNT_REVOCATION_PREFIX + str(account_id))
        return int(value) if value is not None else 0

    def new_session_expires_at(self, opened_at: datetime) -> datetime:
        """Return the effective expiry for a session opened at ``opened_at``."""
        return min(opened_at + self._idle, opened_at + self._absolute)

    def effective_expires_at(self, record: SessionRecord) -> datetime:
        """Return the nearer sliding-idle or fixed-absolute expiry."""
        idle_expiry = datetime.fromisoformat(record.last_seen_at) + self._idle
        absolute_expiry = datetime.fromisoformat(record.absolute_expires_at)
        return min(idle_expiry, absolute_expiry)

    async def resolve(self, raw: str) -> SessionRecord | None:
        """Look up a session and slide its idle clock.

        Returns None for unknown, expired, or revoked — the caller answers
        `401 session-invalid` for all three without distinguishing them.
        """
        token_hash = hash_token(raw)
        key = _SESSION_PREFIX + token_hash
        revocation_key = _SESSION_REVOCATION_PREFIX + token_hash
        if await self._client.exists(revocation_key):
            await self._client.delete(key)
            return None
        payload = await self._client.get(key)
        if payload is None:
            return None

        record = _decode(payload)
        instant = now()

        if (
            self.effective_expires_at(record) <= instant
            or await self.revocation_epoch(UUID(record.account_id))
            != record.revocation_epoch
        ):
            await self._remove_record(key, record)
            return None

        slid = replace(record, last_seen_at=instant.isoformat())
        await self._client.set(key, json.dumps(asdict(slid)), ex=self._ttl_seconds(slid, instant))
        if (
            await self._client.exists(revocation_key)
            or await self.revocation_epoch(UUID(record.account_id))
            != record.revocation_epoch
        ):
            await self._remove_record(key, record)
            return None
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
        old_token_hash = hash_token(raw)
        old_key = _SESSION_PREFIX + old_token_hash
        old_revocation_key = _SESSION_REVOCATION_PREFIX + old_token_hash
        payload = await self._client.get(old_key)
        if payload is None:
            return None

        original = _decode(payload)
        instant = now()
        if (
            self.effective_expires_at(original) <= instant
            or await self.revocation_epoch(UUID(original.account_id))
            != original.revocation_epoch
        ):
            await self._remove_record(old_key, original)
            return None

        record = replace(original, gate=gate)
        new_raw = mint_token()
        new_key = _SESSION_PREFIX + hash_token(new_raw)
        index = _ACCOUNT_INDEX_PREFIX + record.account_id

        async with self._client.pipeline(transaction=True) as pipe:
            pipe.set(new_key, json.dumps(asdict(record)), ex=self._ttl_seconds(record, instant))
            pipe.sadd(index, new_key)
            pipe.srem(index, old_key)
            pipe.set(
                old_revocation_key,
                "1",
                ex=int(self._absolute.total_seconds()),
            )
            pipe.delete(old_key)
            await pipe.execute()

        if await self.revocation_epoch(UUID(record.account_id)) != record.revocation_epoch:
            await self._remove_record(new_key, record)
            return None
        return new_raw

    async def revoke(self, raw: str) -> None:
        """End one session — sign-out."""
        token_hash = hash_token(raw)
        key = _SESSION_PREFIX + token_hash
        revocation_key = _SESSION_REVOCATION_PREFIX + token_hash
        payload = await self._client.get(key)
        if payload is None:
            async with self._client.pipeline(transaction=True) as pipe:
                pipe.set(
                    revocation_key,
                    "1",
                    ex=int(self._absolute.total_seconds()),
                )
                pipe.delete(key)
                await pipe.execute()
            return
        record = _decode(payload)
        async with self._client.pipeline(transaction=True) as pipe:
            pipe.set(
                revocation_key,
                "1",
                ex=int(self._absolute.total_seconds()),
            )
            pipe.delete(key)
            pipe.srem(_ACCOUNT_INDEX_PREFIX + record.account_id, key)
            await pipe.execute()

    async def revoke_all(self, account_id: UUID) -> int:
        """End every session an account holds, immediately.

        This is FR-IDA-010's teeth. Called on deactivation, on a team change that
        alters scope, and on password reset.

        Returns:
            How many sessions were ended.
        """
        index = _ACCOUNT_INDEX_PREFIX + str(account_id)
        epoch_key = _ACCOUNT_REVOCATION_PREFIX + str(account_id)
        async with self._client.pipeline(transaction=True) as pipe:
            pipe.incr(epoch_key)
            pipe.expire(epoch_key, int(self._absolute.total_seconds()))
            await pipe.execute()
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

    async def _remove_record(self, key: str, record: SessionRecord) -> None:
        """Delete a session and its account-index membership atomically."""
        async with self._client.pipeline(transaction=True) as pipe:
            pipe.delete(key)
            pipe.srem(_ACCOUNT_INDEX_PREFIX + record.account_id, key)
            await pipe.execute()


@dataclass(frozen=True, slots=True)
class OpsSessionRecord:
    """A separately keyed operations session with no customer scope fields."""

    ops_account_id: str
    created_at: str
    last_seen_at: str
    absolute_expires_at: str
    revocation_epoch: int = 0


def _decode_ops(payload: str | bytes) -> OpsSessionRecord:
    raw = json.loads(payload)
    known = set(OpsSessionRecord.__dataclass_fields__)
    return OpsSessionRecord(**{key: value for key, value in raw.items() if key in known})


class OpsSessionStore:
    """Opaque operations sessions isolated from customer keys and records."""

    def __init__(self, client: Valkey, *, idle_seconds: int, absolute_seconds: int) -> None:
        self._client = client
        self._idle = timedelta(seconds=idle_seconds)
        self._absolute = timedelta(seconds=absolute_seconds)

    async def create(
        self,
        *,
        ops_account_id: UUID,
        opened_at: datetime | None = None,
        revocation_epoch: int | None = None,
    ) -> str:
        """Open an operations session and return its one-time raw identifier."""
        raw = mint_token()
        instant = opened_at or now()
        epoch = (
            await self.revocation_epoch(ops_account_id)
            if revocation_epoch is None
            else revocation_epoch
        )
        record = OpsSessionRecord(
            ops_account_id=str(ops_account_id),
            created_at=instant.isoformat(),
            last_seen_at=instant.isoformat(),
            absolute_expires_at=(instant + self._absolute).isoformat(),
            revocation_epoch=epoch,
        )
        key = _OPS_SESSION_PREFIX + hash_token(raw)
        index = _OPS_ACCOUNT_INDEX_PREFIX + str(ops_account_id)
        epoch_key = _OPS_ACCOUNT_REVOCATION_PREFIX + str(ops_account_id)
        async with self._client.pipeline(transaction=True) as pipe:
            pipe.set(key, json.dumps(asdict(record)), ex=self._ttl_seconds(record, instant))
            pipe.sadd(index, key)
            pipe.expire(index, int(self._absolute.total_seconds()))
            pipe.expire(epoch_key, int(self._absolute.total_seconds()))
            await pipe.execute()
        if await self.revocation_epoch(ops_account_id) != epoch:
            await self._remove_record(key, record)
            raise SessionRevokedDuringCreation("operator sessions were revoked during issue")
        return raw

    async def revocation_epoch(self, ops_account_id: UUID) -> int:
        """Return the operator generation captured by a prospective session."""
        value = await self._client.get(
            _OPS_ACCOUNT_REVOCATION_PREFIX + str(ops_account_id)
        )
        return int(value) if value is not None else 0

    def new_session_expires_at(self, opened_at: datetime) -> datetime:
        return min(opened_at + self._idle, opened_at + self._absolute)

    def effective_expires_at(self, record: OpsSessionRecord) -> datetime:
        idle_expiry = datetime.fromisoformat(record.last_seen_at) + self._idle
        absolute_expiry = datetime.fromisoformat(record.absolute_expires_at)
        return min(idle_expiry, absolute_expiry)

    async def resolve(self, raw: str) -> OpsSessionRecord | None:
        """Resolve and slide a live operations session."""
        token_hash = hash_token(raw)
        key = _OPS_SESSION_PREFIX + token_hash
        revocation_key = _OPS_SESSION_REVOCATION_PREFIX + token_hash
        if await self._client.exists(revocation_key):
            await self._client.delete(key)
            return None
        payload = await self._client.get(key)
        if payload is None:
            return None
        try:
            record = _decode_ops(payload)
            UUID(record.ops_account_id)
            instant = now()
            if (
                self.effective_expires_at(record) <= instant
                or await self.revocation_epoch(UUID(record.ops_account_id))
                != record.revocation_epoch
            ):
                await self._remove_record(key, record)
                return None
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            await self._client.delete(key)
            return None

        slid = replace(record, last_seen_at=instant.isoformat())
        await self._client.set(
            key, json.dumps(asdict(slid)), ex=self._ttl_seconds(slid, instant)
        )
        if (
            await self._client.exists(revocation_key)
            or await self.revocation_epoch(UUID(record.ops_account_id))
            != record.revocation_epoch
        ):
            await self._remove_record(key, record)
            return None
        return slid

    async def revoke(self, raw: str) -> None:
        """End one operations session."""
        token_hash = hash_token(raw)
        key = _OPS_SESSION_PREFIX + token_hash
        revocation_key = _OPS_SESSION_REVOCATION_PREFIX + token_hash
        payload = await self._client.get(key)
        if payload is None:
            async with self._client.pipeline(transaction=True) as pipe:
                pipe.set(
                    revocation_key,
                    "1",
                    ex=int(self._absolute.total_seconds()),
                )
                pipe.delete(key)
                await pipe.execute()
            return
        try:
            record = _decode_ops(payload)
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            async with self._client.pipeline(transaction=True) as pipe:
                pipe.set(
                    revocation_key,
                    "1",
                    ex=int(self._absolute.total_seconds()),
                )
                pipe.delete(key)
                await pipe.execute()
            return
        async with self._client.pipeline(transaction=True) as pipe:
            pipe.set(
                revocation_key,
                "1",
                ex=int(self._absolute.total_seconds()),
            )
            pipe.delete(key)
            pipe.srem(_OPS_ACCOUNT_INDEX_PREFIX + record.ops_account_id, key)
            await pipe.execute()

    async def revoke_all(self, ops_account_id: UUID) -> int:
        """End every session for an operations account."""
        index = _OPS_ACCOUNT_INDEX_PREFIX + str(ops_account_id)
        epoch_key = _OPS_ACCOUNT_REVOCATION_PREFIX + str(ops_account_id)
        async with self._client.pipeline(transaction=True) as pipe:
            pipe.incr(epoch_key)
            pipe.expire(epoch_key, int(self._absolute.total_seconds()))
            await pipe.execute()
        keys: set[str] = await self._client.smembers(index)  # type: ignore[misc]
        if not keys:
            return 0
        async with self._client.pipeline(transaction=True) as pipe:
            pipe.delete(*keys)
            pipe.delete(index)
            await pipe.execute()
        return len(keys)

    def _ttl_seconds(self, record: OpsSessionRecord, instant: datetime) -> int:
        absolute_remaining = datetime.fromisoformat(record.absolute_expires_at) - instant
        return max(1, int(min(self._idle, absolute_remaining).total_seconds()))

    async def _remove_record(self, key: str, record: OpsSessionRecord) -> None:
        async with self._client.pipeline(transaction=True) as pipe:
            pipe.delete(key)
            pipe.srem(_OPS_ACCOUNT_INDEX_PREFIX + record.ops_account_id, key)
            await pipe.execute()
