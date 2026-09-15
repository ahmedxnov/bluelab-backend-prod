"""Fact extraction from an uploaded source document (FR-KNW-003), via C-8.

Status transitions `received -> extracting -> extracted | failed`; re-running a
terminal upload is a no-op. On failure or an empty extraction nothing reaches
review and the live facts are untouched (FR-KNW-008).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.adapters.document_extraction import (
    FactExtractionProvider,
    create_fact_extraction_provider,
)
from bluelab.adapters.object_store import ObjectRef, ObjectStore, create_object_store
from bluelab.modules.knowledge import service
from bluelab.modules.knowledge.schemas import FactInput
from bluelab.platform.config import Settings
from bluelab.platform.queue.catalog import Lane
from bluelab.platform.queue.compat import JobEnvelope
from bluelab.platform.queue.runtime import JobRegistration, NonRetryableJobFailure


def registration(
    settings: Settings,
    *,
    extractor: FactExtractionProvider | None = None,
    object_store: ObjectStore | None = None,
) -> JobRegistration:
    provider = extractor or create_fact_extraction_provider(settings)
    store = object_store or create_object_store(settings)

    async def handle(session: AsyncSession, job: JobEnvelope) -> None:
        upload_id = UUID(str(job.args["upload_id"]))
        basis = await service.extraction_basis(session, upload_id=upload_id)
        if basis is None:
            return
        data = await store.get(ObjectRef(str(basis["object_key"])))
        suffix = str(basis["filename"]).lower().rsplit(".", 1)[-1]
        content_types = {
            "pdf": "application/pdf",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        }
        facts = await provider.extract(data, content_type=content_types[suffix])
        if not facts:
            raise NonRetryableJobFailure("extraction returned no facts")
        await service.apply_extracted_facts(
            session,
            upload_id=upload_id,
            facts=[FactInput.model_validate(fact.model_dump()) for fact in facts],
        )

    async def exhausted(session: AsyncSession, job: JobEnvelope) -> None:
        await service.mark_extraction_failed(
            session, upload_id=UUID(str(job.args["upload_id"]))
        )

    return JobRegistration(
        lane=Lane.EXTRACT_FACTS, handler=handle, on_exhausted=exhausted
    )
