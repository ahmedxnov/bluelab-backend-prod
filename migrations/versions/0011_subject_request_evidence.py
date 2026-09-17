"""Keep subject-rights evidence after organization deletion.

Revision ID: 0011_subject_request_evidence
Revises: 0010_lifecycle_command_replay
"""

from __future__ import annotations

from alembic import op

revision = "0011_subject_request_evidence"
down_revision = "0010_lifecycle_command_replay"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Align both request projections with the retained-evidence contract."""
    op.execute("alter table erasure_request drop constraint fk_erasure_request_org_id")
    op.execute("alter table export_request drop constraint fk_export_request_org_id")
    op.execute("alter table erasure_request drop constraint ck_erasure_request_status_valid")
    op.execute("alter table export_request drop constraint ck_export_request_status_valid")
    op.execute(
        "alter table erasure_request add constraint ck_erasure_request_status_valid "
        "check (status in ('pending','processing','awaiting_input','executed','failed','rejected','withdrawn'))"
    )
    op.execute(
        "alter table export_request add constraint ck_export_request_status_valid "
        "check (status in ('pending','processing','awaiting_input','ready','delivered','failed','rejected','withdrawn'))"
    )
    op.execute(
        "alter table erasure_request "
        "add column request_policy_reference text not null default 'subject-rights:v1', "
        "add column response_due_at timestamptz, "
        "add column restriction_id uuid, "
        "add column closed_at timestamptz, "
        "add column closed_by uuid references ops_account(id)"
    )
    op.execute(
        "alter table export_request "
        "add column request_policy_reference text not null default 'subject-rights:v1', "
        "add column response_due_at timestamptz, "
        "add column restriction_id uuid, "
        "add column delivered_at timestamptz, "
        "add column closed_at timestamptz, "
        "add column closed_by uuid references ops_account(id)"
    )
    op.execute("alter table erasure_request alter column request_policy_reference drop default")
    op.execute("alter table export_request alter column request_policy_reference drop default")


def downgrade() -> None:
    """BlueLab migrations are forward-only."""
    raise NotImplementedError("BlueLab migrations are forward-only")
