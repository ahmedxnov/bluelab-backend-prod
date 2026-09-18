"""Reserve a transaction-bound freeze capability for authorized purge batches.

Revision ID: 0017_purge_guard_capability
Revises: 0016_org_purge_evidence
"""

from __future__ import annotations

from alembic import op

revision = "0017_purge_guard_capability"
down_revision = "0016_org_purge_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Keep the capability outside all application and maintenance grants."""
    op.execute(
        "create table bluelab_internal.purge_authorization ("
        "backend_pid integer not null, xact_id bigint not null, "
        "purge_step_id uuid not null, primary key (backend_pid, xact_id))"
    )
    op.execute("revoke all on bluelab_internal.purge_authorization from public")
    op.execute("revoke all on bluelab_internal.purge_authorization from bluelab_app")
    op.execute("revoke all on bluelab_internal.purge_authorization from bluelab_maintenance")
    op.execute(
        "alter table org_purge_step add column target_table text, "
        "add column batch_keys jsonb not null default '[]'::jsonb"
    )
    op.execute(
        "alter table org_purge_run add column failure_count integer not null default 0, "
        "add column retry_at timestamptz, add column last_progress_at timestamptz"
    )
    op.execute(
        "alter table org_lifecycle_operation "
        "drop constraint ck_org_lifecycle_operation_action_valid"
    )
    op.execute(
        "alter table org_lifecycle_operation add constraint "
        "ck_org_lifecycle_operation_action_valid check (action in ("
        "'confirm_term','renew_term','expire_term','start','cancel','extend',"
        "'policy_revision','create_restriction','release_restriction',"
        "'set_subject_request_state','claim','authorize_destructive_step',"
        "'complete_destructive_step','complete','record_post_completion_restriction'))"
    )


def downgrade() -> None:
    """BlueLab migrations are forward-only."""
    raise NotImplementedError("BlueLab migrations are forward-only")
