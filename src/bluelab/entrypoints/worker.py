"""Work-plane entrypoint over configured, independently scalable job lanes."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

import procrastinate
from valkey.asyncio import Valkey

from bluelab.adapters.lifecycle_history import create_lifecycle_history
from bluelab.calls.suspension import quiesce_org_calls
from bluelab.platform.config import Plane, Settings, get_settings
from bluelab.platform.db.engine import (
    require_erasure_database_role,
    require_maintenance_database_role,
)
from bluelab.platform.queue.catalog import Lane
from bluelab.platform.queue.runtime import JobRegistration, build_worker_app
from bluelab.platform.telemetry import logging
from bluelab.work.dispatch_email import registration as email_registration
from bluelab.work.extract_facts import registration as extraction_registration
from bluelab.work.generate_rubric import registration as rubric_registration
from bluelab.work.generate_scenario import registration as scenario_registration
from bluelab.work.grade_attempt import registration as grading_registration
from bluelab.work.maintenance import register_maintenance
from bluelab.work.org_purge import register_org_purge
from bluelab.work.org_terms import transition_due
from bluelab.work.render_report import registration as report_registration
from bluelab.work.subject_rights import erasure_registration, export_registration


def create_worker(settings: Settings | None = None) -> procrastinate.App:
    """Compose only the handlers implemented by this release."""
    resolved = settings or get_settings()
    if Lane.EXECUTE_ERASURE.value in resolved.worker_queue_names:
        if resolved.worker_queue_names != (Lane.EXECUTE_ERASURE.value,):
            raise RuntimeError("execute_erasure requires a dedicated worker lane")
        require_erasure_database_role(resolved)
    if "maintenance" in resolved.worker_queue_names:
        require_maintenance_database_role(resolved)
    factories: dict[Lane, Callable[[Settings], JobRegistration]] = {
        Lane.DISPATCH_EMAIL: email_registration,
        Lane.EXTRACT_FACTS: extraction_registration,
        Lane.GENERATE_SCENARIO: scenario_registration,
        Lane.GENERATE_RUBRIC: rubric_registration,
        Lane.GRADE_ATTEMPT: grading_registration,
        Lane.RENDER_REPORT: report_registration,
        Lane.EXECUTE_ERASURE: erasure_registration,
        Lane.EXECUTE_EXPORT: export_registration,
    }
    registrations = [
        factories[Lane(name)](resolved)
        for name in resolved.worker_queue_names if name != "maintenance"
    ]
    app = build_worker_app(
        resolved.database_url.get_secret_value(),
        registrations,
        queues=resolved.worker_queue_names,
        concurrency=resolved.worker_concurrency,
        compatibility_delay_seconds=resolved.worker_compatibility_delay_seconds,
    )
    if "maintenance" in resolved.worker_queue_names:
        history = create_lifecycle_history(resolved)
        @app.periodic(cron="0 * * * *")
        @app.task(name="transition_due_org_terms", queue="maintenance")
        async def transition_due_org_terms(timestamp: int) -> None:
            valkey = Valkey.from_url(
                resolved.valkey_url.get_secret_value(), decode_responses=True,
            )
            try:
                async def quiesce(org_id: UUID, since: datetime) -> None:
                    await quiesce_org_calls(
                        org_id, since=since, valkey=valkey, settings=resolved,
                    )

                await transition_due(history, quiesce=quiesce)
            finally:
                await valkey.aclose()

        register_maintenance(app, resolved)

        async def purge_quiesce(
            org_id: UUID, since: datetime, valkey: Valkey, settings: Settings,
        ) -> None:
            await quiesce_org_calls(
                org_id, since=since, valkey=valkey, settings=settings,
            )

        register_org_purge(app, resolved, history, purge_quiesce)
    return app


def main() -> None:
    """Run the worker with Procrastinate's SIGTERM-aware 30-second drain."""
    settings = get_settings()
    if settings.plane is not Plane.WORKER:
        raise RuntimeError("BLUELAB_PLANE=worker is required for the worker entrypoint")
    logging.configure(settings)
    create_worker(settings).run_worker()


if __name__ == "__main__":
    main()
