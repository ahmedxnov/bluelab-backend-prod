"""Worker payload compatibility and finite lane retry behavior."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, Self, cast
from uuid import UUID

import pytest
from procrastinate import JobContext
from procrastinate.jobs import Job
from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.entrypoints import worker_health
from bluelab.platform.queue import context as queue_context
from bluelab.platform.queue import runtime
from bluelab.platform.queue.catalog import Lane, spec_for, validate_payload
from bluelab.platform.queue.compat import (
    ENVELOPE_VERSION,
    IncompatiblePayload,
    envelope,
    read,
)
from bluelab.platform.queue.context import OrganizationWorkSuspended, job_transaction
from bluelab.platform.queue.runtime import (
    JobExecutor,
    JobRegistration,
    LaneRetryStrategy,
    RetryableJobFailure,
    TerminalJobFailure,
)

pytestmark = pytest.mark.l1_unit

ORG = UUID("01936d54-7ad5-7000-8000-000000000001")
SEND = UUID("01936d54-7ad5-7000-8000-000000000002")


def _context(*, attempts: int) -> JobContext:
    job = Job(
        id=42,
        queue=Lane.DISPATCH_EMAIL.value,
        lock=None,
        queueing_lock=None,
        task_name=Lane.DISPATCH_EMAIL.value,
        task_kwargs={},
        attempts=attempts,
    )
    return JobContext(
        app=cast(Any, None),
        job=job,
        start_timestamp=0,
        abort_reason=lambda: None,
    )


@pytest.mark.verifies("ADR-0039")
def test_envelope_reader_tolerates_additions_but_defers_a_newer_version():
    body = envelope({"email_send_id": str(SEND)}, org_id=ORG)
    body["added_envelope_field"] = "ignored"
    cast(dict[str, object], body["args"])["future_id"] = str(SEND)

    decoded = read(body)
    assert decoded.args["future_id"] == str(SEND)
    validate_payload(Lane.DISPATCH_EMAIL, decoded.args)

    newer = {**body, "v": ENVELOPE_VERSION + 1}
    with pytest.raises(IncompatiblePayload):
        read(newer)


@pytest.mark.verifies("ADR-0023")
def test_newer_envelope_is_never_exhausted_as_a_business_failure():
    strategy = LaneRetryStrategy(Lane.DISPATCH_EMAIL, compatibility_delay_seconds=5)
    decision = strategy.get_retry_decision(
        exception=IncompatiblePayload(),
        job=_context(attempts=10_000).job,
    )
    assert decision is not None
    assert decision.retry_at is not None


@pytest.mark.verifies("ADR-0023")
def test_business_failure_retry_stops_at_the_lane_budget():
    strategy = LaneRetryStrategy(Lane.DISPATCH_EMAIL)
    before = strategy.get_retry_decision(
        exception=RetryableJobFailure(),
        job=_context(attempts=spec_for(Lane.DISPATCH_EMAIL).max_attempts - 1).job,
    )
    exhausted = strategy.get_retry_decision(
        exception=RetryableJobFailure(),
        job=_context(attempts=spec_for(Lane.DISPATCH_EMAIL).max_attempts).job,
    )
    assert before is not None
    assert exhausted is None


@pytest.mark.verifies("ADR-0023")
async def test_terminal_action_runs_after_failed_attempt_rollback(monkeypatch):
    events: list[str] = []
    body = envelope({"email_send_id": str(SEND)}, org_id=ORG)

    @asynccontextmanager
    async def fake_transaction(*_args: object, **_kwargs: object):
        events.append("transaction_open")
        try:
            yield cast(AsyncSession, object()), read(body)
        except Exception:
            events.append("transaction_rollback")
            raise
        else:
            events.append("transaction_commit")

    async def handler(_session: AsyncSession, _job: object) -> None:
        events.append("handler")
        raise RuntimeError("private provider detail")

    async def exhausted(_session: AsyncSession, _job: object) -> None:
        events.append("exhausted")

    monkeypatch.setattr(runtime, "job_transaction", fake_transaction)

    async def no_erasure_fence(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(runtime, "subject_is_erased", no_erasure_fence)
    executor = JobExecutor(
        JobRegistration(
            lane=Lane.DISPATCH_EMAIL,
            handler=handler,
            on_exhausted=exhausted,
        )
    )

    with pytest.raises(TerminalJobFailure, match="retry budget exhausted") as caught:
        await executor(
            _context(attempts=spec_for(Lane.DISPATCH_EMAIL).max_attempts),
            body,
        )

    assert caught.value.__cause__ is None
    assert events == [
        "transaction_open",
        "handler",
        "transaction_rollback",
        "transaction_open",
        "exhausted",
        "transaction_commit",
    ]


@pytest.mark.verifies("FR-IDA-014")
async def test_worker_rolls_back_when_service_ends_before_commit(monkeypatch):
    events: list[str] = []
    allowed = iter((True, False))

    @asynccontextmanager
    async def fake_transaction(_scope: object):
        try:
            yield cast(AsyncSession, object())
        except OrganizationWorkSuspended:
            events.append("rollback")
            raise
        else:
            events.append("commit")

    async def fake_access(_session: AsyncSession) -> bool:
        events.append("access_check")
        return next(allowed)

    monkeypatch.setattr(queue_context, "scoped_transaction", fake_transaction)
    monkeypatch.setattr(queue_context, "org_service_access_allowed", fake_access)
    body = envelope({"email_send_id": str(SEND)}, org_id=ORG)
    with pytest.raises(OrganizationWorkSuspended):
        async with job_transaction(Lane.DISPATCH_EMAIL, body, job_id="test"):
            events.append("handler")
    assert events == ["access_check", "handler", "access_check", "rollback"]


@pytest.mark.verifies("FR-IDA-014")
async def test_suspended_worker_does_not_run_handler_or_exhaustion(monkeypatch):
    calls: list[str] = []

    @asynccontextmanager
    async def suspended_transaction(*_args: object, **_kwargs: object):
        raise OrganizationWorkSuspended
        yield cast(AsyncSession, object()), read({})  # pragma: no cover

    async def handler(_session: AsyncSession, _job: object) -> None:
        calls.append("handler")

    async def exhausted(_session: AsyncSession, _job: object) -> None:
        calls.append("exhausted")

    monkeypatch.setattr(runtime, "job_transaction", suspended_transaction)
    executor = JobExecutor(
        JobRegistration(
            lane=Lane.DISPATCH_EMAIL, handler=handler, on_exhausted=exhausted
        )
    )
    await executor(_context(attempts=spec_for(Lane.DISPATCH_EMAIL).max_attempts),
                   envelope({"email_send_id": str(SEND)}, org_id=ORG))
    assert calls == []


@pytest.mark.verifies("ADR-0023")
def test_worker_health_uses_the_queue_heartbeat(monkeypatch):
    class FakeResult:
        def fetchone(self) -> tuple[bool]:
            return (True,)

    class FakeConnection:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, statement: str) -> FakeResult:
            assert "procrastinate_workers" in statement
            return FakeResult()

    monkeypatch.setattr(
        worker_health.psycopg,
        "connect",
        lambda *_args, **_kwargs: FakeConnection(),
    )

    assert worker_health.heartbeat_is_current(
        "postgresql+asyncpg://worker:secret@postgres/bluelab"  # pragma: allowlist secret -- synthetic URL
    )
