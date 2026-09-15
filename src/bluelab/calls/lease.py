"""Participant-keyed single-live-call leases (ADR-0011, FR-LIV-005)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from typing import Literal, cast
from uuid import UUID

from valkey.asyncio import Valkey


class LeaseState(StrEnum):
    PENDING = "pending"
    ESTABLISHED = "established"


ParticipantKind = Literal["rep", "candidate", "author"]


@dataclass(frozen=True, slots=True)
class CallLease:
    participant_kind: ParticipantKind
    participant_id: UUID
    call_id: UUID
    attempt_id: UUID | None
    state: LeaseState

    @classmethod
    def pending(
        cls,
        *,
        participant_kind: ParticipantKind,
        participant_id: UUID,
        call_id: UUID,
        attempt_id: UUID | None,
    ) -> CallLease:
        return cls(
            participant_kind, participant_id, call_id, attempt_id, LeaseState.PENDING
        )


class CallLeaseStore:
    """Atomic ownership checks over a lease shared by every token and device."""

    def __init__(self, client: Valkey, *, ttl_seconds: int = 120) -> None:
        self._client = client
        self._ttl_seconds = ttl_seconds

    @staticmethod
    def key(kind: ParticipantKind, participant_id: UUID) -> str:
        # An account remains one participant when switching between an authored
        # feel-check and a scored rep call; tokens and tabs cannot split it.
        namespace = "candidate" if kind == "candidate" else "account"
        return f"call:participant:{namespace}:{participant_id}"

    @staticmethod
    def _encode(lease: CallLease) -> str:
        value = asdict(lease)
        return json.dumps(
            {
                key: str(item) if isinstance(item, (UUID, StrEnum)) else item
                for key, item in value.items()
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _decode(raw: str) -> CallLease:
        value = json.loads(raw)
        return CallLease(
            participant_kind=cast(ParticipantKind, value["participant_kind"]),
            participant_id=UUID(value["participant_id"]),
            call_id=UUID(value["call_id"]),
            attempt_id=UUID(value["attempt_id"]) if value["attempt_id"] else None,
            state=LeaseState(value["state"]),
        )

    async def acquire(self, lease: CallLease) -> bool:
        return bool(
            await self._client.set(
                self.key(lease.participant_kind, lease.participant_id),
                self._encode(lease),
                ex=self._ttl_seconds,
                nx=True,
            )
        )

    async def current(
        self, kind: ParticipantKind, participant_id: UUID
    ) -> CallLease | None:
        raw = await self._client.get(self.key(kind, participant_id))
        return self._decode(raw) if raw else None

    async def renew(self, lease: CallLease) -> bool:
        """Re-create/refresh only when the caller still owns the participant lease."""
        key = self.key(lease.participant_kind, lease.participant_id)
        current = await self._client.get(key)
        if current is None or self._decode(current).call_id != lease.call_id:
            return False
        await self._client.set(key, self._encode(lease), ex=self._ttl_seconds)
        return True

    async def mark_established(self, lease: CallLease) -> bool:
        return await self.renew(replace(lease, state=LeaseState.ESTABLISHED))

    async def release(self, lease: CallLease) -> bool:
        key = self.key(lease.participant_kind, lease.participant_id)
        while True:
            async with self._client.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(key)
                    current = await pipe.get(key)
                    if (
                        current is None
                        or self._decode(current).call_id != lease.call_id
                    ):
                        await pipe.reset()  # type: ignore[no-untyped-call]
                        return False
                    pipe.multi()  # type: ignore[no-untyped-call]
                    pipe.delete(key)
                    await pipe.execute()
                    return True
                except Exception as exc:
                    if exc.__class__.__name__ != "WatchError":
                        raise
