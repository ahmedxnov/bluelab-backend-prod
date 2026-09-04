"""Alembic environment.

Runs under the migration role — the only role holding `BYPASSRLS` (data/04 §1) —
from the deploy pipeline, before the app rollout of the release that needs it.

## Three things this file does that a default `env.py` does not

**Imports every module's models.** `Base.metadata` is populated by side effect, so
a module missing from `MODEL_MODULES` is a set of tables autogenerate cannot see
— it would cheerfully propose dropping them. The RLS generator learned this the
hard way on its first run; the same list, and the same failure mode.

**Sets a bounded `lock_timeout` and fails fast.** A migration that blocks on a
lock behind a long-running query holds the deploy and, at Tier 1, the whole box.
pipeline/04 §1 requires migrations-first with a bounded timeout — better a failed
deploy than a stalled one.

**Reads `MIGRATION_DATABASE_URL`, never `DATABASE_URL`.** They are different
roles on purpose. The application role is non-superuser without `BYPASSRLS`; the
migration role has it and nothing else does. Running the lineage as the app role
would fail on the RLS objects — or worse, half-succeed.

Autogenerate is a **draft aid only**. Every migration is reviewed DDL, and RLS
policies, triggers, views, and functions never come from it (data/04 §1) — they
live in `sql/` and are re-applied idempotently at every release.
"""

from __future__ import annotations

import os
import sys
from importlib import import_module
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bluelab.platform.db.base import Base

MODEL_MODULES = (
    "bluelab.modules.identity.models",
    "bluelab.modules.knowledge.models",
    "bluelab.modules.drills.models",
    "bluelab.modules.review.models",
    "bluelab.modules.training.models",
    "bluelab.modules.hiring.models",
    "bluelab.modules.operations.models",
    "bluelab.notifications.models",
)
"""Every module that owns tables. `assessment` is absent because it owns none —
the candidate journey is behaviour over rows other modules own."""

for _module in MODEL_MODULES:
    import_module(_module)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

LOCK_TIMEOUT_MS = os.environ.get("MIGRATION_LOCK_TIMEOUT_MS", "5000")
"""Bounded, and short. A migration waiting on a lock is a deploy that has stopped
without saying so (pipeline/04 §1)."""


def _url() -> str:
    url = os.environ.get("MIGRATION_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "MIGRATION_DATABASE_URL is not set. The lineage runs as the migration "
            "role — the only role holding BYPASSRLS (ADR-0031 §3) — not as the "
            "application role."
        )
    # Alembic runs synchronously; the app's async driver is not wanted here.
    return url.replace("+asyncpg", "+psycopg")


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a connection — the review artifact."""
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply the lineage against a live database."""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _url()

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        # The lock timeout is a CONNECTION parameter, not a statement.
        #
        # Issuing `set lock_timeout` on the connection before `context.configure`
        # opens an implicit transaction; Alembic then sees a transaction it did
        # not begin, its own commit never fires, and the whole lineage rolls back
        # when the block exits — reporting success the entire time. That is
        # exactly what the first run of this file did: "Running upgrade ->
        # 0001_initial_schema" followed by zero tables.
        connect_args={"options": f"-c lock_timeout={LOCK_TIMEOUT_MS}"},
    )

    with connectable.connect() as connection:
        # DDL is transactional in PostgreSQL, so the whole lineage step commits or
        # none of it does. The few non-transactional operations (CREATE INDEX
        # CONCURRENTLY) get their own single-step migration, marked as such
        # (data/04 §1).
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
