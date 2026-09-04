"""Engine and pooling.

Assumes a transaction-mode pooler (RDS Proxy, Supavisor, or none depending on the
host), so session state must be per-transaction only — which is exactly what the
`SET LOCAL` scope context of ADR-0031 is designed for (data/04 §8).

Two driver settings follow directly from that assumption and are easy to get
wrong:

* **`statement_cache_size=0`.** asyncpg prepares statements server-side and keys
  the cache to a backend connection. Behind a transaction-mode pooler the next
  transaction may land on a different backend, and the cached plan is gone —
  producing `InvalidSQLStatementNameError` under load and only under load. The
  cache must be off.
* **A statement timeout, set per connection.** Without one, a pathological query
  holds a pooled connection until the client gives up. With the pool sized for a
  single small host at T1, a handful of those is the whole plane.

Nothing here knows which host it is talking to. The engine is PostgreSQL 16 and
the URL is configuration — that is the portability rule (ADR-0022, data/04 §8).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from bluelab.platform.config import Settings, get_settings


def build_engine(settings: Settings) -> AsyncEngine:
    """Create the application-role engine.

    The role this connects as is **non-superuser and does not hold BYPASSRLS**;
    only the migration role does (ADR-0031 decision 3). The clone test asserts
    that as its first check, precisely so a restore that skipped the role
    bootstrap cannot yield a false green where RLS "passes" only because the
    roles that enforce it were never created (SEC-041).
    """
    # `connect_args` goes to SQLAlchemy's asyncpg ADAPTER, which consumes some
    # keys itself and forwards the rest to `asyncpg.connect()`.
    #
    # `prepared_statement_cache_size` is one it consumes — verified against
    # `AsyncAdapt_asyncpg_dbapi.connect`, which names it explicitly. It is NOT a
    # dialect argument: `DefaultDialect.__init__` does not accept it, and passing
    # it to `create_async_engine` raises
    # `TypeError: Invalid argument(s) 'prepared_statement_cache_size'` before a
    # connection is ever attempted.
    #
    # Do not confuse it with asyncpg's own `statement_cache_size`. That one the
    # adapter already pins to 0 and manages itself, and overriding it here breaks
    # the adapter's bookkeeping.
    connect_args: dict[str, Any] = {
        # The prepared-statement cache, disabled: behind a transaction-mode pooler
        # the next transaction may land on a different backend and the cached plan
        # is gone — an error that appears only under load.
        "prepared_statement_cache_size": 0,
        "server_settings": {
            "application_name": f"bluelab-{settings.plane.value}",
            "statement_timeout": str(settings.db_statement_timeout_ms),
            # Bound how long a transaction may sit idle holding row locks. The
            # nine invariant transactions are short by construction; anything
            # that idles inside one is a bug, and this makes it a loud one.
            "idle_in_transaction_session_timeout": "30000",
        },
    }

    return create_async_engine(
        settings.database_url.get_secret_value(),
        echo=settings.db_echo,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_pool_max_overflow,
        pool_pre_ping=True,
        # Recycle below the shortest idle timeout any pooler or load balancer in
        # front of us is likely to impose, so a stale socket surfaces here rather
        # than as a mid-transaction disconnect.
        pool_recycle=1_800,
        connect_args=connect_args,
    )


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    """The process-wide engine. One per process, created lazily."""
    return build_engine(get_settings())


async def dispose_engine() -> None:
    """Close the pool on shutdown.

    Called from the application lifespan and from the worker's shutdown hook so a
    drain does not leave sockets open past the drain window (infra/02 §3).
    """
    engine = get_engine()
    await engine.dispose()
