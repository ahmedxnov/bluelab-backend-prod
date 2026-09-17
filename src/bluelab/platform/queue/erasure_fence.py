"""Permanent subject fences checked by every ordinary work lane."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.platform.queue.catalog import Lane


async def subject_is_erased(
    session: AsyncSession, *, lane: Lane, payload: dict[str, object]
) -> bool:
    """Lock each target subject and reject work after any erasure request.

    The advisory lock is the common serialization boundary with request
    creation. A request that wins the lock becomes a permanent fence; a worker
    that wins first is allowed to commit before the request is accepted.
    """
    if lane in {Lane.EXECUTE_ERASURE, Lane.EXECUTE_EXPORT}:
        return False

    identities = await _subject_identities(session, lane=lane, payload=payload)
    for kind, subject_id in sorted(identities, key=lambda item: str(item[1])):
        await session.execute(
            text("select pg_advisory_xact_lock(hashtextextended(:subject, 0))"),
            {"subject": str(subject_id)},
        )
        fenced = await session.scalar(
            text(
                "select exists(select 1 from erasure_request "
                "where subject_kind=:kind and subject_id=:subject)"
            ),
            {"kind": kind, "subject": subject_id},
        )
        if fenced:
            return True
    return False


async def _subject_identities(
    session: AsyncSession, *, lane: Lane, payload: dict[str, object]
) -> set[tuple[str, UUID]]:
    if lane is Lane.GRADE_ATTEMPT:
        row = (
            await session.execute(
                text(
                    "select rep_account_id,candidate_id from attempt where id=:id"
                ),
                {"id": UUID(str(payload["attempt_id"]))},
            )
        ).mappings().one_or_none()
        if row is None:
            return set()
        if row["rep_account_id"] is not None:
            return {("account", row["rep_account_id"])}
        return {("candidate", row["candidate_id"])}

    if lane in {Lane.GENERATE_SCENARIO, Lane.GENERATE_RUBRIC}:
        subject = await session.scalar(
            text("select author_account_id from drill where id=:id"),
            {"id": UUID(str(payload["drill_id"]))},
        )
        return {("account", subject)} if subject is not None else set()

    if lane is Lane.EXTRACT_FACTS:
        subject = await session.scalar(
            text("select created_by from document_upload where id=:id"),
            {"id": UUID(str(payload["upload_id"]))},
        )
        return {("account", subject)} if subject is not None else set()

    if lane is Lane.RENDER_REPORT:
        return {("candidate", UUID(str(payload["candidate_id"])))}

    if lane is Lane.DISPATCH_EMAIL:
        row = (
            await session.execute(
                text(
                    "select account_id,candidate_id from email_send where id=:id"
                ),
                {"id": UUID(str(payload["email_send_id"]))},
            )
        ).mappings().one_or_none()
        if row is None:
            return set()
        return {
            (kind, subject_id)
            for kind, subject_id in (
                ("account", row["account_id"]),
                ("candidate", row["candidate_id"]),
            )
            if subject_id is not None
        }

    return set()
