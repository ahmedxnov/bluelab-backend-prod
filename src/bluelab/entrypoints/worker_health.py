"""Content-free liveness probe for the Procrastinate worker container."""

from __future__ import annotations

import os

import psycopg

from bluelab.platform.queue.runtime import database_conninfo

_CURRENT_HEARTBEAT = """
select coalesce(
    bool_or(last_heartbeat >= clock_timestamp() - interval '30 seconds'),
    false
)
from procrastinate_workers
"""


def heartbeat_is_current(database_url: str) -> bool:
    """Return whether the queue has a worker heartbeat inside its stall window."""
    try:
        conninfo = database_conninfo(database_url)
        with psycopg.connect(conninfo, connect_timeout=2) as connection:
            row = connection.execute(_CURRENT_HEARTBEAT).fetchone()
    except (psycopg.Error, ValueError):
        return False
    return row is not None and row[0] is True


def main() -> int:
    """Exit non-zero when configuration, PostgreSQL, or the heartbeat is absent."""
    database_url = os.environ.get("DATABASE_URL")
    if database_url is None:
        return 1
    return 0 if heartbeat_is_current(database_url) else 1


if __name__ == "__main__":
    raise SystemExit(main())
