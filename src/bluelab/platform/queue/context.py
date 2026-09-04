"""Scope resolution for workers: the job row carries the scope, and the worker sets
the same transaction-local GUCs a request would (ADR-0031 decision 1).

## The work plane gets no unscoped path

ADR-0005 records this as an explicit negative consequence to be honored, not a
nice-to-have: a worker running unscoped would be a second way to read across a
tenant boundary, and it would be invisible because no user is watching a job run.

So a worker's unit of work is the same shape as a request's — a `ScopeContext`
into `scoped_transaction()`. The only difference is where the scope comes from:
a request resolves it from a credential, a job reads it off the envelope.

Workers use `PrincipalKind.SYSTEM`, which the policy matrix grants "scoped"
access on every class (ADR-0031 §4) — broad within an org, and **bounded by the
org**. A worker is not ops: it has no verb-scoped cross-org reach.

## Why the scope is on the envelope rather than looked up

A worker could re-derive scope by loading the row named in the payload. That
would be a query made *before* any scope is set — which is exactly the unscoped
path this design forbids. Carrying the scope on the envelope means the very first
statement of the transaction is already bounded.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.platform.db.privileged import system_scope
from bluelab.platform.db.scope import ScopeContext
from bluelab.platform.db.session import scoped_transaction
from bluelab.platform.queue.catalog import Lane
from bluelab.platform.queue.compat import JobEnvelope, read
from bluelab.platform.telemetry import correlation


def scope_from_envelope(job: JobEnvelope) -> ScopeContext:
    """Build the worker's scope context from the job envelope."""
    return system_scope(org_id=job.org_id, team_id=job.team_id)


@asynccontextmanager
async def job_transaction(
    lane: Lane, body: dict[str, Any], *, job_id: str
) -> AsyncIterator[tuple[AsyncSession, JobEnvelope]]:
    """Open a scoped transaction for one job execution.

    Binds the correlation spine first, so every log line, span, and metric sample
    the job emits carries `job_id` and `job_lane` and joins with the rest of the
    unit of work (observability/01 §3).

    Args:
        lane: The lane being executed.
        body: The raw envelope from the job row.
        job_id: The queue's job id, for correlation.

    Yields:
        The scoped session and the decoded envelope.

    Raises:
        IncompatiblePayload: If the envelope is newer than this release. The
            caller must **leave the job for another worker**, not fail it —
            during a rollback window, workers of two releases poll the same lane.

    Example:
        async with job_transaction(Lane.GRADE_ATTEMPT, body, job_id=jid) as (session, job):
            attempt_id = UUID(job.args["attempt_id"])
            ...
    """
    envelope = read(body)
    scope = scope_from_envelope(envelope)

    correlation.bind(
        correlation.Correlation(
            request_id=correlation.new_request_id(),
            plane="work",
            org_id=envelope.org_id,
            job_id=job_id,
            job_lane=lane.value,
        )
    )

    async with scoped_transaction(scope) as session:
        yield session, envelope
