"""Application-owned audio recording orchestration and reconciliation."""

from __future__ import annotations

import aiohttp
from livekit import api
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from valkey.asyncio import Valkey

from bluelab.adapters.object_store import (
    DeletionReason,
    ObjectRef,
    create_object_store,
)
from bluelab.calls.registry import CallSession
from bluelab.platform.config import Settings
from bluelab.platform.ids import new_id
from bluelab.platform.telemetry import metrics


class EgressRegistry:
    """Idempotent recording start and reordered-outcome coordination."""

    def __init__(self, client: Valkey, *, ttl_seconds: int = 7_200) -> None:
        self._client = client
        self._ttl_seconds = ttl_seconds

    @staticmethod
    def _id_key(call_id: object) -> str:
        return f"call:egress:{call_id}"

    @staticmethod
    def _outcome_key(call_id: object) -> str:
        return f"call:egress-outcome:{call_id}"

    async def claim_start(self, call: CallSession) -> bool:
        return bool(
            await self._client.set(
                self._id_key(call.call_id),
                "starting",
                ex=self._ttl_seconds,
                nx=True,
            )
        )

    async def started(self, call: CallSession, egress_id: str) -> None:
        await self._client.set(
            self._id_key(call.call_id), egress_id, ex=self._ttl_seconds
        )

    async def release_start(self, call: CallSession) -> None:
        if await self._client.get(self._id_key(call.call_id)) == "starting":
            await self._client.delete(self._id_key(call.call_id))

    async def matches(self, call: CallSession, egress_id: str) -> bool:
        expected = await self._client.get(self._id_key(call.call_id))
        return bool(expected and expected != "starting" and expected == egress_id)

    async def set_outcome(self, call: CallSession, *, available: bool) -> None:
        await self._client.set(
            self._outcome_key(call.call_id),
            "available" if available else "unavailable",
            ex=self._ttl_seconds,
        )

    async def outcome(self, call: CallSession) -> bool | None:
        value = await self._client.get(self._outcome_key(call.call_id))
        if value == "available":
            return True
        if value == "unavailable":
            return False
        return None


async def start_recording(settings: Settings, call: CallSession) -> str | None:
    """Start one audio-only room composite with application-owned credentials."""
    if call.mode == "test" or call.attempt_id is None:
        return None
    if (
        not settings.livekit_url
        or settings.livekit_api_key is None
        or settings.livekit_api_secret is None
    ):
        return None
    ref = ObjectRef.recording(org_id=call.org_id, attempt_id=call.attempt_id)
    storage = api.S3Upload(
        access_key=settings.object_store_access_key.get_secret_value()
        if settings.object_store_access_key
        else "",
        secret=settings.object_store_secret_key.get_secret_value()
        if settings.object_store_secret_key
        else "",
        region=settings.object_store_region,
        endpoint=settings.object_store_endpoint or "",
        bucket=settings.object_store_bucket,
        force_path_style=bool(settings.object_store_endpoint),
    )
    start = api.RoomCompositeEgressRequest(
        room_name=f"call_{call.call_id}",
        audio_only=True,
        file_outputs=[
            api.EncodedFileOutput(
                file_type=api.EncodedFileType.OGG, filepath=ref.key, s3=storage
            )
        ],
    )
    client = api.LiveKitAPI(
        settings.livekit_url,
        settings.livekit_api_key.get_secret_value(),
        settings.livekit_api_secret.get_secret_value(),
        timeout=aiohttp.ClientTimeout(total=settings.dependency_timeout_seconds),
    )
    try:
        result = await client.egress.start_room_composite_egress(start)
        return str(result.egress_id)
    finally:
        await client.aclose()


async def reconcile_recording(
    session: AsyncSession, call: CallSession, *, available: bool
) -> bool:
    """Apply a terminal egress callback once; failures become operator-visible."""
    if call.attempt_id is None:
        return False
    state = "available" if available else "unavailable"
    result = await session.execute(
        text(
            "update attempt set recording_status=:state where id=:attempt and recording_status='pending'"
        ),
        {"attempt": call.attempt_id, "state": state},
    )
    changed = bool(result.rowcount)  # type: ignore[attr-defined]
    if changed and not available:
        await session.execute(
            text("""insert into ops_fault(id,org_id,kind,attempt_id,detail)
                      select :id,:org,'playback_asset',:attempt,'{}'::jsonb
                       where not exists(select 1 from ops_fault where kind='playback_asset'
                         and attempt_id=:attempt and status='open')"""),
            {"id": new_id(), "org": call.org_id, "attempt": call.attempt_id},
        )
    if changed:
        metrics.record_call_recovery(outcome=f"recording_{state}")
    return changed


async def attempt_status(session: AsyncSession, call: CallSession) -> str | None:
    if call.attempt_id is None:
        return None
    return (
        await session.execute(
            text("select status from attempt where id=:attempt"),
            {"attempt": call.attempt_id},
        )
    ).scalar_one_or_none()


async def recording_status(session: AsyncSession, call: CallSession) -> str | None:
    if call.attempt_id is None:
        return None
    return (
        await session.execute(
            text("select recording_status from attempt where id=:attempt"),
            {"attempt": call.attempt_id},
        )
    ).scalar_one_or_none()


async def delete_orphan_recording(settings: Settings, call: CallSession) -> None:
    """Delete only the deterministic recording key through the audited sweep path."""
    if call.attempt_id is None:
        return
    await create_object_store(settings).delete(
        ObjectRef.recording(org_id=call.org_id, attempt_id=call.attempt_id),
        reason=DeletionReason.SWEEP,
    )
    metrics.record_call_recovery(outcome="orphan_cleaned")
