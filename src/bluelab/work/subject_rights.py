"""Idempotent subject export and erasure worker registrations."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.adapters.erasure_ledger import ErasureLedger, create_erasure_ledger
from bluelab.adapters.object_store import ObjectStore, create_object_store
from bluelab.modules.operations import subject_rights
from bluelab.platform.config import Settings
from bluelab.platform.queue.catalog import Lane
from bluelab.platform.queue.compat import JobEnvelope
from bluelab.platform.queue.runtime import JobRegistration, NonRetryableJobFailure


def erasure_registration(
    settings: Settings,
    *,
    ledger: ErasureLedger | None = None,
    object_store: ObjectStore | None = None,
) -> JobRegistration:
    resolved_ledger = ledger or create_erasure_ledger(settings)
    store = object_store or create_object_store(settings)

    async def handle(session: AsyncSession, job: JobEnvelope) -> None:
        try:
            await subject_rights.execute_erasure(
                session,
                request_id=UUID(str(job.args["request_id"])),
                ledger=resolved_ledger,
                object_store=store,
            )
        except subject_rights.SubjectRequestUnavailable:
            raise NonRetryableJobFailure("subject request unavailable") from None

    async def exhausted(session: AsyncSession, job: JobEnvelope) -> None:
        await subject_rights.fail_erasure(
            session, request_id=UUID(str(job.args["request_id"]))
        )

    return JobRegistration(
        lane=Lane.EXECUTE_ERASURE, handler=handle, on_exhausted=exhausted
    )


def export_registration(
    settings: Settings, *, object_store: ObjectStore | None = None
) -> JobRegistration:
    store = object_store or create_object_store(settings)

    async def handle(session: AsyncSession, job: JobEnvelope) -> None:
        try:
            await subject_rights.execute_export(
                session,
                request_id=UUID(str(job.args["request_id"])),
                object_store=store,
            )
        except subject_rights.SubjectRequestUnavailable:
            raise NonRetryableJobFailure("subject request unavailable") from None

    async def exhausted(session: AsyncSession, job: JobEnvelope) -> None:
        await subject_rights.fail_export(
            session, request_id=UUID(str(job.args["request_id"]))
        )

    return JobRegistration(
        lane=Lane.EXECUTE_EXPORT, handler=handle, on_exhausted=exhausted
    )
