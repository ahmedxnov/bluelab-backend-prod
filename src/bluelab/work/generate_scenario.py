"""Scenario and persona generation (FR-DRL-004/008). Keyed on `request_id` so a
stale generation never overwrites a newer one. On failure the status carries the
reason, there is **no fallback content**, and publish stays blocked (FR-DRL-006).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.adapters.generation_llm import (
    GenerationProvider,
    create_generation_provider,
    validate_scenario_basis,
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
            session, drill_id=drill_id, request_id=request_id, kind="scenario"
        )
        if basis is None:
            return
        result = await resolved.generate_scenario(basis)
        validate_scenario_basis(result, basis)
        await service.apply_scenario(
            session, drill_id=drill_id, request_id=request_id, result=result
        )

    async def exhausted(session: AsyncSession, job: JobEnvelope) -> None:
        await service.mark_generation_failed(
            session,
            drill_id=UUID(str(job.args["drill_id"])),
            request_id=UUID(str(job.args["request_id"])),
            kind="scenario",
        )

    return JobRegistration(
        lane=Lane.GENERATE_SCENARIO, handler=handle, on_exhausted=exhausted
    )
