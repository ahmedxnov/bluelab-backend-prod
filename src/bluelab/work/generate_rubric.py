"""Rubric generation (FR-DRL-011) — replaces the rubric wholesale on success. Weights
remain editable before publish and must sum to 100 at the T-5 gate.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.adapters.generation_llm import (
    GenerationProvider,
    create_generation_provider,
)
from bluelab.modules.drills import service
from bluelab.platform.config import Settings
from bluelab.platform.queue.catalog import Lane
from bluelab.platform.queue.compat import JobEnvelope
from bluelab.platform.queue.runtime import JobRegistration


def registration(
    settings: Settings, *, provider: GenerationProvider | None = None
) -> JobRegistration:
    resolved = provider or create_generation_provider(settings)

    async def handle(session: AsyncSession, job: JobEnvelope) -> None:
        drill_id, request_id = (
            UUID(str(job.args["drill_id"])),
            UUID(str(job.args["request_id"])),
        )
        basis = await service.generation_basis(
            session, drill_id=drill_id, request_id=request_id, kind="rubric"
        )
        if basis is None:
            return
        result = await resolved.generate_rubric(basis)
        await service.apply_rubric(
            session, drill_id=drill_id, request_id=request_id, result=result
        )

    async def exhausted(session: AsyncSession, job: JobEnvelope) -> None:
        await service.mark_generation_failed(
            session,
            drill_id=UUID(str(job.args["drill_id"])),
            request_id=UUID(str(job.args["request_id"])),
            kind="rubric",
        )

    return JobRegistration(
        lane=Lane.GENERATE_RUBRIC, handler=handle, on_exhausted=exhausted
    )
