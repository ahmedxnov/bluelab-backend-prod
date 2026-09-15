"""The ops fault queue (ADR-0010): grading failures (FR-SCR-009) and unavailable
playback assets (FR-SCR-013).

Content-free by construction — a fault carries identifiers and a reason, never the
content that produced it, because ops staff must be able to diagnose without
reading customer content. Resolution re-drives the job by identity.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.modules.operations.schemas import CursorPage, FaultPage, FaultView
from bluelab.modules.operations.service import append_audit, require_reason
from bluelab.platform.errors import catalog
from bluelab.platform.errors.denial import ProblemError, not_found
from bluelab.platform.queue.catalog import Lane
from bluelab.platform.queue.enqueue import enqueue

FaultFilter = Literal["open", "resolved", "all"]


@dataclass(frozen=True, slots=True)
class FaultCursor:
    status: FaultFilter
    opened_at: datetime
    fault_id: UUID

    def encode(self) -> str:
        payload = json.dumps(
            {
                "v": 1,
                "status": self.status,
                "opened_at": self.opened_at.isoformat(),
                "fault_id": str(self.fault_id),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return base64.urlsafe_b64encode(payload).decode()

    @classmethod
    def decode(cls, cursor: str, *, status: FaultFilter) -> FaultCursor:
        try:
            raw = base64.b64decode(cursor.encode(), altchars=b"-_", validate=True)
            data = json.loads(raw)
            if set(data) != {"v", "status", "opened_at", "fault_id"} or data["v"] != 1:
                raise ValueError
            decoded = cls(
                status=data["status"],
                opened_at=datetime.fromisoformat(data["opened_at"]),
                fault_id=UUID(data["fault_id"]),
            )
        except (
            ValueError,
            TypeError,
            UnicodeDecodeError,
            binascii.Error,
            json.JSONDecodeError,
        ) as exc:
            raise ProblemError(
                catalog.VALIDATION_ERROR,
                detail="cursor is not one this server issued",
            ) from exc
        if decoded.status != status:
            raise ProblemError(
                catalog.VALIDATION_ERROR,
                detail="cursor does not belong to this fault filter",
            )
        return decoded


def _safe_detail(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    detail: dict[str, Any] = {}
    error_class = value.get("error_class")
    retry_count = value.get("retry_count")
    if isinstance(error_class, str) and 0 < len(error_class) <= 100:
        detail["error_class"] = error_class
    if isinstance(retry_count, int) and 0 <= retry_count <= 100:
        detail["retry_count"] = retry_count
    return detail


def fault_view(row: Mapping[str, Any]) -> FaultView:
    return FaultView(
        id=row["id"],
        org_id=row["org_id"],
        kind=row["kind"],
        attempt_id=row["attempt_id"],
        status=row["status"],
        detail=_safe_detail(row["detail"]),
        opened_at=row["opened_at"],
        resolved_at=row["resolved_at"],
        resolution_note=row["resolution_note"],
    )


_LIST = text(
    """
    select id,org_id,kind,attempt_id,status,detail,opened_at,resolved_at,resolution_note
    from ops_fault
    where (:status='all' or status=:status)
      and (
        cast(:cursor_at as timestamptz) is null
        or (opened_at,id) > (cast(:cursor_at as timestamptz),cast(:cursor_id as uuid))
      )
    order by opened_at,id
    limit :limit
    """
)


async def list_faults(
    session: AsyncSession,
    *,
    status: FaultFilter,
    cursor: str | None,
    limit: int,
) -> FaultPage:
    position = FaultCursor.decode(cursor, status=status) if cursor is not None else None
    rows = (
        (
            await session.execute(
                _LIST,
                {
                    "status": status,
                    "cursor_at": position.opened_at if position else None,
                    "cursor_id": position.fault_id if position else None,
                    "limit": limit + 1,
                },
            )
        )
        .mappings()
        .all()
    )
    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = (
        FaultCursor(
            status=status,
            opened_at=page[-1]["opened_at"],
            fault_id=page[-1]["id"],
        ).encode()
        if has_more and page
        else None
    )
    return FaultPage(
        data=[fault_view(dict(row)) for row in page],
        pagination=CursorPage(next_cursor=next_cursor, has_more=has_more),
    )


_GET = text(
    """
    select id,org_id,kind,attempt_id,status,detail,opened_at,resolved_at,resolution_note
    from ops_fault where id=:fault
    """
)


async def load_fault(session: AsyncSession, *, fault_id: UUID) -> FaultView:
    row = ((await session.execute(_GET, {"fault": fault_id})).mappings().first())
    if row is None:
        raise not_found()
    return fault_view(dict(row))


_LOCK = text(
    """
    select id,org_id,kind,attempt_id,status,detail,opened_at,resolved_at,resolution_note
    from ops_fault where id=:fault for update
    """
)

_RESOLVE = text(
    """
    update ops_fault
       set status='resolved',resolved_at=now(),
           resolved_by=nullif(current_setting('app.ops_account_id',true),'')::uuid,
           resolution_note=:reason
     where id=:fault
    returning id,org_id,kind,attempt_id,status,detail,opened_at,resolved_at,resolution_note
    """
)


async def resolve_fault(
    session: AsyncSession,
    *,
    fault_id: UUID,
    reason: str,
    playback_available: bool | None = None,
) -> FaultView:
    row = ((await session.execute(_LOCK, {"fault": fault_id})).mappings().first())
    if row is None:
        raise not_found()
    if row["status"] == "resolved":
        raise ProblemError(catalog.FAULT_ALREADY_RESOLVED)
    normalized_reason = require_reason(reason)
    kind = cast(str, row["kind"])
    if kind == "grading_failure":
        await enqueue(
            session,
            Lane.GRADE_ATTEMPT,
            {"attempt_id": str(row["attempt_id"])},
            org_id=row["org_id"],
        )
    elif kind == "playback_asset":
        if playback_available is None:
            raise ValueError("playback re-drive requires an identity-only asset check")
        outcome = (
            await session.execute(
                text("select app_redrive_playback_fault(:fault,:available)"),
                {"fault": fault_id, "available": playback_available},
            )
        ).scalar_one()
        if outcome != "updated":
            raise RuntimeError("playback fault re-drive did not update its attempt")
    else:
        raise RuntimeError("fault kind is outside the closed inventory")

    resolved = (
        (
            await session.execute(
                _RESOLVE, {"fault": fault_id, "reason": normalized_reason}
            )
        )
        .mappings()
        .one()
    )
    await append_audit(
        session,
        verb="resolve_fault",
        target_org_id=row["org_id"],
        target_ref={
            "fault_id": str(fault_id),
            "attempt_id": str(row["attempt_id"]),
            "kind": kind,
            "redrive": "enqueued" if kind == "grading_failure" else "asset_rechecked",
        },
        reason=normalized_reason,
    )
    return fault_view(dict(resolved))
