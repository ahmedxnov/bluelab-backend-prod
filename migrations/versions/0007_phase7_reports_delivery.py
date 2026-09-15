"""Phase 7 restricted report delivery snapshots.

Revision ID: 0007_phase7_reports_delivery
Revises: 0006_phase6_entry_prerequisites
"""

from __future__ import annotations

from alembic import op

revision = "0007_phase7_reports_delivery"
down_revision = "0006_phase6_entry_prerequisites"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add immutable T-9 candidate attachment snapshots and robust freeze support."""
    op.execute("alter table shortlist add column candidate_snapshot jsonb not null default '[]'::jsonb")
    op.execute("""
        create unique index uq_shortlist_candidate_once
        on shortlist_candidate(candidate_id)
    """)


def downgrade() -> None:
    raise NotImplementedError("BlueLab migrations are forward-only")
