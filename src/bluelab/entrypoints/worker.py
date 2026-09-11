"""Work-plane entrypoint over configured, independently scalable job lanes."""

from __future__ import annotations

import procrastinate

from bluelab.platform.config import Plane, Settings, get_settings
from bluelab.platform.queue.runtime import build_worker_app
from bluelab.platform.telemetry import logging
from bluelab.work.dispatch_email import registration as email_registration


def create_worker(settings: Settings | None = None) -> procrastinate.App:
    """Compose only the handlers implemented by this release."""
    resolved = settings or get_settings()
    registrations = [email_registration(resolved)]
    return build_worker_app(
        resolved.database_url.get_secret_value(),
        registrations,
        queues=resolved.worker_queue_names,
        concurrency=resolved.worker_concurrency,
        compatibility_delay_seconds=resolved.worker_compatibility_delay_seconds,
    )


def main() -> None:
    """Run the worker with Procrastinate's SIGTERM-aware 30-second drain."""
    settings = get_settings()
    if settings.plane is not Plane.WORKER:
        raise RuntimeError("BLUELAB_PLANE=worker is required for the worker entrypoint")
    logging.configure(settings)
    create_worker(settings).run_worker()


if __name__ == "__main__":
    main()
