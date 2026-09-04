"""Transactional enqueue — the one property the whole queue choice rests on.

ADR-0023 picked a Postgres-backed queue over SQS or RabbitMQ for exactly one
reason, and it is not throughput: **the job row commits with the domain write.**

    "write the attempt row + enqueue grading"     commit together
    "freeze the drill + enqueue nothing partial"  commit together

That is the outbox problem every external broker reintroduces at these volumes,
solved by not having the problem. Concretely it is what makes T-2 a single
transaction — transcript insert, status flip, and `grade_attempt` enqueue
committing as one (data/02 §1) — and the `grade_attempt` insert **is** the call
completion event, the only true event in v1 (architecture/00 §4).

So the rule for every caller: **enqueue inside the domain transaction, never
after it.** An enqueue that happens after the commit has silently recreated the
outbox problem and will strand work on a crash between the two.

## The pinned-API obligation (quality/08 §4, data F-9)

Procrastinate's `defer` API is pinned against the pinned library version, and
this module is where that pin lives. The distinction the banked item draws is
worth keeping: **the transactional-enqueue property is the commitment; the
library's syntax is schematic.** So the property gets an L3 integration test —
kill the transaction, assert no job row survives — and the syntax gets a version
pin. If the library changes its call shape, this file changes; nothing else does.

Procrastinate's own migrations are pinned to the library version and wrapped as
steps of our Alembic lineage, so a restore plus `alembic upgrade head` always
converges (data/04 §4).

## The escape hatch, kept cheap

SQS (Milan) is the named escape if queue behaviour ever measurably pressures the
primary. It stays cheap only if the worker interface stays broker-agnostic — so
callers depend on this module, never on Procrastinate directly.

Payloads carry **ids only, never content** (api/02 §2): every worker re-reads
current truth, which is what makes at-least-once retry safe, and what keeps the N
/ N+1 payload window narrow (see `compat.py`).
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.platform.queue.catalog import Lane, validate_payload
from bluelab.platform.queue.compat import envelope

_DEFER = text(
    """
    insert into procrastinate_jobs (queue_name, task_name, args, status)
    values (:queue_name, :task_name, cast(:args as jsonb), 'todo')
    returning id
    """
)
"""The pinned `defer` form.

Written as SQL against Procrastinate's table rather than through its Python API
for one reason: `defer()` manages its own connection, and a job deferred on a
*different* connection does not participate in our transaction — which would
break the single commit this module exists to guarantee. The table shape is
pinned to the library version, and the L3 test that kills the transaction and
asserts no job row survives is what keeps the pin honest.
"""


async def enqueue(
    session: AsyncSession,
    lane: Lane,
    payload: dict[str, Any],
    *,
    org_id: UUID,
    team_id: UUID | None = None,
) -> int:
    """Enqueue a job **inside the caller's transaction**.

    Args:
        session: The session of the domain transaction in progress. Passing a
            different session — or calling after the commit — reintroduces the
            outbox problem this design exists to avoid.
        lane: One of the six. A name outside the catalogue is refused.
        payload: Ids only. Validated, not trusted.
        org_id: The scope the worker will resolve from the job row. Required:
            the work plane gets no unscoped path (ADR-0005, ADR-0031).
        team_id: The team scope, where the job has one.

    Returns:
        The job id, for correlation.

    Example:
        async with scoped_transaction(scope) as session:
            session.add(attempt)
            await session.flush()
            await enqueue(
                session, Lane.GRADE_ATTEMPT, {"attempt_id": str(attempt.id)},
                org_id=scope.org_id, team_id=scope.team_id,
            )
        # transcript, status flip, and the job row commit together — or none do
    """
    validate_payload(lane, payload)
    body = envelope(payload, org_id=org_id, team_id=team_id)

    result = await session.execute(
        _DEFER,
        {
            "queue_name": lane.value,
            "task_name": lane.value,
            # Serialised here, not passed as a dict: the parameter is bound as
            # text and cast to jsonb in SQL, and asyncpg will not bind a mapping
            # to a text parameter.
            "args": json.dumps(body, separators=(",", ":")),
        },
    )
    return int(result.scalar_one())
