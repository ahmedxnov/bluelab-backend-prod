"""Set-based RC-3…RC-6, RC-8, and RC-9 retention sweeps."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.adapters.object_store import DeletionReason, ObjectRef, ObjectStore
from bluelab.platform.telemetry import metrics
from bluelab.platform.telemetry.logging import get_logger

RetentionClass = Literal["RC-3", "RC-4", "RC-5", "RC-6", "RC-8", "RC-9"]
# RC-7 is deliberately absent: data/03 §7 excludes automatic evidence purges,
# and erasure_request remains a worker fence for the life of its org.
RETENTION_CLASSES: tuple[RetentionClass, ...] = (
    "RC-3",
    "RC-4",
    "RC-5",
    "RC-6",
    "RC-8",
    "RC-9",
)
_log = get_logger(__name__)


async def sweep(
    session: AsyncSession,
    *,
    retention_class: RetentionClass,
    object_store: ObjectStore,
    at: datetime | None = None,
) -> dict[str, object]:
    """Apply one class atomically; object failure rolls database deletes back."""
    try:
        result = dict(
            (
                await session.execute(
                    text("select app_retention_sweep(:class,:at)"),
                    {"class": retention_class, "at": at or datetime.now(UTC)},
                )
            ).scalar_one()
        )
        keys = [str(key) for key in result.pop("object_keys", [])]
        for key in keys:
            await object_store.delete(ObjectRef(key), reason=DeletionReason.SWEEP)
    except Exception:
        metrics.record_lifecycle_sweep(
            retention_class=retention_class,
            outcome="failed",
            rows=0,
        )
        _log.error("retention_sweep_failed", retention_class=retention_class)
        raise
    result["objects_deleted"] = len(keys)
    metrics.record_lifecycle_sweep(
        retention_class=retention_class,
        outcome="success",
        rows=int(result["primary_count"]) + int(result["secondary_count"]),
    )
    _log.info("retention_sweep_completed", **result)
    return result


async def sweep_stale_recordings(
    session: AsyncSession, *, at: datetime | None = None
) -> dict[str, int]:
    """Resolve DB-backed pending recordings, including after Valkey state loss."""
    try:
        result = dict(
            (
                await session.execute(
                    text("select app_stale_pending_recordings(:at)"),
                    {"at": at or datetime.now(UTC)},
                )
            ).scalar_one()
        )
    except Exception:
        metrics.record_stale_recording_sweep(outcome="failed", rows=0)
        _log.error("stale_recording_sweep_failed")
        raise
    counts = {key: int(value) for key, value in result.items()}
    metrics.record_stale_recording_sweep(
        outcome="success", rows=counts["attempts_unavailable"]
    )
    if counts["attempts_unavailable"]:
        _log.warning("stale_recordings_reconciled", **counts)
    return counts
