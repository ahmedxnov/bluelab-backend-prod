"""Content-free active-call registry used for recovery and internal callbacks."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from typing import Literal, cast
from uuid import UUID

from valkey.asyncio import Valkey


class SessionState(StrEnum):
    PENDING = "pending"
    ESTABLISHED = "established"
    TERMINAL = "terminal"


@dataclass(frozen=True, slots=True)
class CallSession:
    call_id: UUID
    mode: Literal["attempt", "test"]
    participant_kind: Literal["rep", "candidate", "author"]
    participant_id: UUID
    participant_identity: str
    participant_display_name: str
    org_id: UUID
    team_id: UUID
    drill_id: UUID
    attempt_id: UUID | None
    stage_id: UUID | None = None
    stage_ord: int | None = None
    stage_total: int | None = None
    restart: bool = False
    capacity_slot: int | None = None
    position_id: UUID | None = None
    account_role: Literal["manager", "rep"] | None = None
    state: SessionState = SessionState.PENDING


class CallRegistry:
    _RECOVERY_KEY = "call:recovery"
    _RECORDING_KEY = "call:recording-recovery"

    def __init__(self, client: Valkey, *, ttl_seconds: int = 7_200) -> None:
        self._client = client
        self._ttl_seconds = ttl_seconds

    @staticmethod
    def _key(call_id: UUID) -> str:
        return f"call:session:{call_id}"

    @staticmethod
    def _encode(session: CallSession) -> str:
        raw = asdict(session)
        return json.dumps(
            {
                key: str(value) if isinstance(value, (UUID, StrEnum)) else value
                for key, value in raw.items()
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _decode(raw: str) -> CallSession:
        data = json.loads(raw)
        for field in ("call_id", "participant_id", "org_id", "team_id", "drill_id"):
            data[field] = UUID(data[field])
        for field in ("attempt_id", "stage_id", "position_id"):
            data[field] = UUID(data[field]) if data[field] else None
        data["state"] = SessionState(data["state"])
        return CallSession(**data)

    async def put(
        self, session: CallSession, *, recover_at_epoch: int | None = None
    ) -> None:
        await self._client.set(
            self._key(session.call_id), self._encode(session), ex=self._ttl_seconds
        )
        deadline = recover_at_epoch
        if deadline is None:
            deadline = int(time.time()) + (
                60 if session.state is SessionState.PENDING else 930
            )
        if session.state is SessionState.TERMINAL:
            await self._client.zrem(self._RECOVERY_KEY, str(session.call_id))
        else:
            await self._client.zadd(
                self._RECOVERY_KEY, {str(session.call_id): deadline}
            )

    async def get(self, call_id: UUID) -> CallSession | None:
        raw = await self._client.get(self._key(call_id))
        return self._decode(cast(str, raw)) if raw else None

    async def mark_established(self, session: CallSession) -> CallSession:
        established = replace(session, state=SessionState.ESTABLISHED)
        await self.put(established)
        return established

    async def mark_terminal(self, session: CallSession) -> CallSession:
        terminal = replace(session, state=SessionState.TERMINAL)
        await self.put(terminal)
        if terminal.attempt_id is not None:
            await self._client.zadd(
                self._RECORDING_KEY, {str(terminal.call_id): int(time.time()) + 600}
            )
        return terminal

    async def delete(self, call_id: UUID) -> None:
        await self._client.delete(self._key(call_id))
        await self._client.zrem(self._RECOVERY_KEY, str(call_id))
        await self._client.zrem(self._RECORDING_KEY, str(call_id))

    async def due(self, *, now_epoch: int | None = None) -> list[CallSession]:
        now = int(time.time()) if now_epoch is None else now_epoch
        ids = await self._client.zrangebyscore(self._RECOVERY_KEY, min="-inf", max=now)
        calls: list[CallSession] = []
        for raw_id in ids:
            call = await self.get(UUID(str(raw_id)))
            if call is None:
                await self._client.zrem(self._RECOVERY_KEY, raw_id)
            else:
                calls.append(call)
        return calls

    async def due_recordings(
        self, *, now_epoch: int | None = None
    ) -> list[CallSession]:
        now = int(time.time()) if now_epoch is None else now_epoch
        ids = await self._client.zrangebyscore(self._RECORDING_KEY, min="-inf", max=now)
        calls: list[CallSession] = []
        for raw_id in ids:
            call = await self.get(UUID(str(raw_id)))
            await self._client.zrem(self._RECORDING_KEY, raw_id)
            if call is not None:
                calls.append(call)
        return calls

    async def recording_resolved(self, call_id: UUID) -> None:
        await self._client.zrem(self._RECORDING_KEY, str(call_id))


class CapacitySlots:
    """Configuration-sized SET-NX slots; no application branch names a tier."""

    def __init__(
        self, client: Valkey, *, capacity: int, ttl_seconds: int = 7_200
    ) -> None:
        self._client = client
        self._capacity = capacity
        self._ttl_seconds = ttl_seconds

    async def acquire(self, call_id: UUID) -> int | None:
        for slot in range(self._capacity):
            if await self._client.set(
                f"call:capacity:{slot}", str(call_id), ex=self._ttl_seconds, nx=True
            ):
                return slot
        return None

    async def release(self, call_id: UUID, slot: int | None) -> bool:
        if slot is None:
            return False
        key = f"call:capacity:{slot}"
        current = await self._client.get(key)
        if current != str(call_id):
            return False
        return bool(await self._client.delete(key))

    async def spend_blocked(self) -> bool:
        value = await self._client.get("call:spend:fraction")
        try:
            return value is not None and float(value) >= 1.0
        except ValueError:
            return True
