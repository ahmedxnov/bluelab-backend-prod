"""The idempotent ``render_report`` worker (FR-HIR-011/012)."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.adapters.object_store import ObjectRef, ObjectStore, create_object_store
from bluelab.adapters.pdf_render import (
    PdfRenderer,
    PlaywrightPdfRenderer,
    default_embedded_fonts,
)
from bluelab.adapters.report_takeaway import (
    CrossDrillTakeawayProvider,
    create_cross_drill_takeaway_provider,
)
from bluelab.modules.hiring import reports
from bluelab.platform.config import Settings
from bluelab.platform.queue.catalog import Lane
from bluelab.platform.queue.compat import JobEnvelope
from bluelab.platform.queue.runtime import JobRegistration
from bluelab.platform.telemetry import metrics


def registration(settings: Settings, *, object_store: ObjectStore | None = None, renderer: PdfRenderer | None = None, takeaway_provider: CrossDrillTakeawayProvider | None = None) -> JobRegistration:
    """Bind C-6/C-9 capabilities once; the job payload remains candidate-id only."""
    store = object_store or create_object_store(settings)
    pdf = renderer or PlaywrightPdfRenderer(default_embedded_fonts())
    provider = takeaway_provider or create_cross_drill_takeaway_provider(settings)

    async def handle(session: AsyncSession, job: JobEnvelope) -> None:
        candidate_id = UUID(str(job.args["candidate_id"]))
        projection = await reports.render_projection(session, candidate_id=candidate_id)
        if projection is None:
            return
        if not projection.cards:
            raise RuntimeError("completed report has no graded evidence yet")
        takeaway = projection.stored_takeaway or await provider.synthesize(reports.takeaway_basis(projection))
        pdf_bytes = await pdf.render(reports.render_html(projection, takeaway=takeaway))
        ref = ObjectRef.report(org_id=projection.org_id, candidate_id=candidate_id)
        await store.put(ref, pdf_bytes, content_type="application/pdf")
        if await reports.store_rendered_report(session, candidate_id=candidate_id, takeaway=takeaway, object_key=ref.key):
            metrics.record_report_render(state="available")

    async def exhausted(session: AsyncSession, job: JobEnvelope) -> None:
        await reports.mark_render_failed(session, candidate_id=UUID(str(job.args["candidate_id"])))
        metrics.record_report_render(state="failed")

    return JobRegistration(lane=Lane.RENDER_REPORT, handler=handle, on_exhausted=exhausted)
