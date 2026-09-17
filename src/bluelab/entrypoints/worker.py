"""Work-plane entrypoint over configured, independently scalable job lanes."""

from __future__ import annotations

import procrastinate

from bluelab.adapters.lifecycle_history import create_lifecycle_history
from bluelab.platform.config import Plane, Settings, get_settings
from bluelab.platform.queue.runtime import build_worker_app
from bluelab.platform.telemetry import logging
from bluelab.work.dispatch_email import registration as email_registration
from bluelab.work.extract_facts import registration as extraction_registration
from bluelab.work.generate_rubric import registration as rubric_registration
from bluelab.work.generate_scenario import registration as scenario_registration
from bluelab.work.grade_attempt import registration as grading_registration
from bluelab.work.maintenance import register_maintenance
from bluelab.work.org_terms import transition_due
from bluelab.work.render_report import registration as report_registration
from bluelab.work.subject_rights import erasure_registration, export_registration


def create_worker(settings: Settings | None = None) -> procrastinate.App:
    """Compose only the handlers implemented by this release."""
    resolved = settings or get_settings()
    registrations = [
        email_registration(resolved),
        extraction_registration(resolved),
        scenario_registration(resolved),
        rubric_registration(resolved),
        grading_registration(resolved),
        report_registration(resolved),
        erasure_registration(resolved),
        export_registration(resolved),
    ]
    app = build_worker_app(
        resolved.database_url.get_secret_value(),
        registrations,
        queues=resolved.worker_queue_names,
        concurrency=resolved.worker_concurrency,
        compatibility_delay_seconds=resolved.worker_compatibility_delay_seconds,
    )
    history = create_lifecycle_history(resolved)

    @app.periodic(cron="0 * * * *")
    @app.task(name="transition_due_org_terms", queue="maintenance")
    async def transition_due_org_terms(timestamp: int) -> None:
        await transition_due(history)

    register_maintenance(app, resolved)
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
