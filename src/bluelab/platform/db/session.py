"""The mandatory scoped-session factory — the second belt to RLS (ADR-0022).

There is no unscoped session. Every unit of work opens a transaction whose scope
context has already been applied by `scope.py`; a caller that wants a session
without a resolved principal does not get one.

## Why a factory and not a `get_db()` dependency

The familiar FastAPI shape — `async def get_db() -> AsyncSession` yielding a
bare session — is exactly what this module refuses to expose. A bare session is
an unscoped session, and an unscoped query against a customer-data table is the
thing ADR-0005 requires to be *inexpressible*. So the only way to obtain a
session here is to hand over a `ScopeContext` first.

RLS is the first belt and would deny the query anyway. This is the second belt,
and its value is that the failure is a missing argument at import time rather
than an empty result set at runtime — the difference between a bug you cannot
write and a bug you have to notice.

## The one escape hatch, named

`migration_session` exists for the Alembic lineage and the role bootstrap, which
run as the migration role (`BYPASSRLS`) and legitimately have no principal. It is
not importable from any request path — the module-boundary lint forbids
`bluelab.modules` and `bluelab.api` from reaching it (ADR-0031's enumerated
escape hatches: each one audited, none reachable from a request).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bluelab.platform.db.engine import get_engine
from bluelab.platform.db.scope import ScopeContext, apply_scope


@lru_cache(maxsize=1)
def _sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=get_engine(),
        expire_on_commit=False,
        autoflush=False,
    )


@asynccontextmanager
async def scoped_transaction(scope: ScopeContext) -> AsyncIterator[AsyncSession]:
    """Open one transaction with `scope` applied, and commit or roll it back.

    This is the unit of work for the whole application plane. The nine invariant
    transactions (T-1…T-9, data/02 §1) are each one of these — which is what lets
    "transcript insert, status flip, and grade enqueue commit together" be a
    property of the code rather than an aspiration (ADR-0023).

    Args:
        scope: The resolved principal. Built by `bluelab.api.deps` for a request
            or by `bluelab.platform.queue.context` for a job.

    Yields:
        A session whose transaction already carries the scope GUCs.

    Example:
        async with scoped_transaction(scope) as session:
            session.add(attempt)
            await enqueue(session, "grade_attempt", {"attempt_id": attempt.id})
        # both committed, or neither
    """
    async with _sessionmaker()() as session, session.begin():
        # Before any statement that could touch a customer-data table.
        await apply_scope(session, scope)
        yield session


@asynccontextmanager
async def migration_session() -> AsyncIterator[AsyncSession]:
    """An unscoped session for the migration role only.

    Enumerated escape hatch (ADR-0031). Used by the Alembic lineage, the role
    bootstrap, and the RLS drift check. Never reachable from a request path — the
    import-linter contracts enforce that, not this docstring.
    """
    async with _sessionmaker()() as session, session.begin():
        yield session
