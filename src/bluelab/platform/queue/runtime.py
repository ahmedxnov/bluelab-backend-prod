"""Broker-agnostic worker execution over the Procrastinate transport.

The worker resolves scope before a handler can query, executes one handler in
one scoped transaction, applies finite per-lane retry budgets, and runs each
lane's explicit exhaustion callback in a fresh transaction after the failed
attempt rolls back.  The transport is isolated here so ADR-0023's named SQS
escape does not leak into domain workers.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

import procrastinate
from procrastinate import JobContext
from procrastinate.jobs import Job
from procrastinate.retry import RetryDecision, RetryStrategy
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.platform.queue.catalog import Lane, spec_for, validate_payload
from bluelab.platform.queue.compat import IncompatiblePayload, JobEnvelope
from bluelab.platform.queue.context import OrganizationWorkSuspended, job_transaction
from bluelab.platform.queue.erasure_fence import subject_is_erased
from bluelab.platform.telemetry import metrics
from bluelab.platform.telemetry.logging import get_logger

JobHandler = Callable[[AsyncSession, JobEnvelope], Awaitable[None]]
ExhaustionHandler = Callable[[AsyncSession, JobEnvelope], Awaitable[None]]

_logger = get_logger(__name__)


class RetryableJobFailure(RuntimeError):
    """Content-free marker persisted by Procrastinate for a retryable failure."""


class NonRetryableJobFailure(RuntimeError):
    """Content-free marker for a permanent payload/current-state failure."""


class TerminalJobFailure(RuntimeError):
    """Content-free marker persisted after the exhaustion action commits."""


class ExhaustionActionFailure(RuntimeError):
    """Content-free marker that keeps retrying until terminal state is durable."""


@dataclass(frozen=True, slots=True)
class JobRegistration:
    """One implemented lane and its lane-specific terminal action."""

    lane: Lane
    handler: JobHandler
    on_exhausted: ExhaustionHandler
    exhaustion_outcome: metrics.JobOutcome = metrics.JobOutcome.FAILED


class LaneRetryStrategy(RetryStrategy):
    """Finite business retries; unlimited deferral for a newer envelope.

    Envelope compatibility is not a business failure.  Rescheduling it does not
    consume the lane's finite retry decision and never moves it to ``failed``;
    an upgraded worker can claim it after the short compatibility delay.
    """

    def __init__(
        self,
        lane: Lane,
        *,
        compatibility_delay_seconds: int = 5,
        maximum_delay_seconds: int = 300,
    ) -> None:
        super().__init__()
        self._lane = lane
        self._compatibility_delay_seconds = compatibility_delay_seconds
        self._maximum_delay_seconds = maximum_delay_seconds

    def get_retry_decision(
        self, *, exception: BaseException, job: Job
    ) -> RetryDecision | None:
        if isinstance(exception, IncompatiblePayload):
            return RetryDecision(retry_in={"seconds": self._compatibility_delay_seconds})
        if isinstance(exception, ExhaustionActionFailure):
            return RetryDecision(retry_in={"seconds": self._compatibility_delay_seconds})
        if isinstance(exception, RetryableJobFailure):
            spec = spec_for(self._lane)
            if job.attempts >= spec.max_attempts:
                return None
            delay = min(2 ** (job.attempts + 1), self._maximum_delay_seconds)
            return RetryDecision(retry_in={"seconds": delay})
        return None


class JobExecutor:
    """Execute attempts and keep transport errors free of customer content."""

    def __init__(self, registration: JobRegistration) -> None:
        self._registration = registration

    async def __call__(self, context: JobContext, body: dict[str, Any]) -> None:
        lane = self._registration.lane
        job_id = str(context.job.id)
        started = time.monotonic()
        try:
            async with job_transaction(lane, body, job_id=job_id) as (session, envelope):
                validate_payload(lane, envelope.args)
                if await subject_is_erased(
                    session, lane=lane, payload=envelope.args
                ):
                    _logger.info("job_rejected_by_erasure_fence", lane=lane.value)
                    return
                await self._registration.handler(session, envelope)
        except OrganizationWorkSuspended:
            metrics.record_job(
                lane=lane.value,
                outcome=metrics.JobOutcome.SUCCESS,
                duration_ms=(time.monotonic() - started) * 1000,
            )
            _logger.info("job_rejected_by_org_service_gate", lane=lane.value)
            return
        except IncompatiblePayload:
            metrics.record_job(
                lane=lane.value,
                outcome=metrics.JobOutcome.RETRY,
                duration_ms=(time.monotonic() - started) * 1000,
            )
            _logger.info("job_envelope_deferred", lane=lane.value)
            raise
        except Exception as error:  # noqa: BLE001 -- boundary sanitizes every failure
            duration_ms = (time.monotonic() - started) * 1000
            if isinstance(error, NonRetryableJobFailure) or (
                context.job.attempts >= spec_for(lane).max_attempts
            ):
                try:
                    await self._run_exhaustion(body=body, job_id=job_id)
                except OrganizationWorkSuspended:
                    metrics.record_job(
                        lane=lane.value,
                        outcome=metrics.JobOutcome.SUCCESS,
                        duration_ms=duration_ms,
                    )
                    _logger.info("job_rejected_by_org_service_gate", lane=lane.value)
                    return
                except Exception:  # noqa: BLE001 -- retry sanitized terminal action
                    metrics.record_job(
                        lane=lane.value,
                        outcome=metrics.JobOutcome.RETRY,
                        duration_ms=duration_ms,
                    )
                    _logger.error("job_exhaustion_action_failed", lane=lane.value)
                    raise ExhaustionActionFailure(
                        "job exhaustion action failed"
                    ) from None
                metrics.record_job(
                    lane=lane.value,
                    outcome=self._registration.exhaustion_outcome,
                    duration_ms=duration_ms,
                )
                _logger.error("job_retry_exhausted", lane=lane.value)
                raise TerminalJobFailure("job retry budget exhausted") from None
            metrics.record_job(
                lane=lane.value,
                outcome=metrics.JobOutcome.RETRY,
                duration_ms=duration_ms,
            )
            _logger.warning(
                "job_attempt_failed",
                lane=lane.value,
                attempt=context.job.attempts + 1,
            )
            raise RetryableJobFailure("job attempt failed") from None
        else:
            metrics.record_job(
                lane=lane.value,
                outcome=metrics.JobOutcome.SUCCESS,
                duration_ms=(time.monotonic() - started) * 1000,
            )
            _logger.info("job_succeeded", lane=lane.value)

    async def _run_exhaustion(self, *, body: dict[str, Any], job_id: str) -> None:
        lane = self._registration.lane
        async with job_transaction(lane, body, job_id=job_id) as (session, envelope):
            validate_payload(lane, envelope.args)
            await self._registration.on_exhausted(session, envelope)


def _task_for(executor: JobExecutor) -> Callable[..., Awaitable[None]]:
    """Close over one executor without exposing it as a persisted task argument."""

    async def execute(context: JobContext, /, **body: Any) -> None:
        await executor(context, body)

    return execute


def database_conninfo(database_url: str) -> str:
    """Convert the SQLAlchemy async URL into psycopg's transport URL."""
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise ValueError("the worker queue requires PostgreSQL")
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def build_worker_app(
    database_url: str,
    registrations: Iterable[JobRegistration],
    *,
    queues: Iterable[str],
    concurrency: int,
    compatibility_delay_seconds: int,
) -> procrastinate.App:
    """Build one worker process for its configured per-lane queues."""
    registrations_by_lane = {item.lane: item for item in registrations}
    selected = tuple(dict.fromkeys(queues))
    if not selected:
        raise ValueError("a worker must listen to at least one lane")
    missing = [
        lane for lane in selected
        if lane != "maintenance" and Lane(lane) not in registrations_by_lane
    ]
    if missing:
        raise ValueError(f"worker has no handler for configured lanes: {missing}")

    connector = procrastinate.PsycopgConnector(
        conninfo=database_conninfo(database_url),
        min_size=1,
        max_size=max(2, concurrency + 1),
    )
    app = procrastinate.App(
        connector=connector,
        periodic_defaults={"max_delay": 86_400.0},
        worker_defaults={
            "queues": list(selected),
            "concurrency": concurrency,
            "shutdown_graceful_timeout": 30.0,
            "delete_jobs": "never",
            "wait": True,
        },
    )

    for lane in selected:
        if lane == "maintenance":
            continue
        registration = registrations_by_lane[Lane(lane)]
        executor = JobExecutor(registration)
        execute = _task_for(executor)
        execute.__name__ = f"execute_{lane}"
        app.task(
            name=lane,
            queue=lane,
            pass_context=True,
            retry=LaneRetryStrategy(
                Lane(lane),
                compatibility_delay_seconds=compatibility_delay_seconds,
            ),
        )(execute)

    return app
