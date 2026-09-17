"""Phase 9 lifecycle and erasure compatibility.

Revision ID: 0008_phase9_lifecycle
Revises: 0007_phase7_reports_delivery
"""

from __future__ import annotations

from alembic import op

revision = "0008_phase9_lifecycle"
down_revision = "0007_phase7_reports_delivery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Permit credential nulling and add lifecycle selection indexes."""
    op.execute("alter table account alter column password_hash drop not null")
    op.execute(
        "create index if not exists idx_erasure_subject "
        "on erasure_request(org_id,subject_kind,subject_id)"
    )
    op.execute(
        "create index if not exists idx_export_expiry "
        "on export_request(expires_at) where bundle_object_key is not null"
    )
    op.execute(
        "create index if not exists idx_request_queue "
        "on erasure_request(status,requested_at,id)"
    )


def downgrade() -> None:
    raise NotImplementedError("BlueLab migrations are forward-only")
