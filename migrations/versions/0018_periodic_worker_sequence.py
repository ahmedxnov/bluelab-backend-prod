"""Let the maintenance worker schedule its periodic jobs.

Revision ID: 0018_periodic_worker_sequence
Revises: 0017_purge_guard_capability
"""

from __future__ import annotations

from alembic import op

revision = "0018_periodic_worker_sequence"
down_revision = "0017_purge_guard_capability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Grant only the periodic deferral allocator to the scheduling lane."""
    op.execute(
        "grant usage, select on sequence procrastinate_periodic_defers_id_seq "
        "to bluelab_maintenance"
    )


def downgrade() -> None:
    """BlueLab migrations are forward-only."""
    raise NotImplementedError("BlueLab migrations are forward-only")
