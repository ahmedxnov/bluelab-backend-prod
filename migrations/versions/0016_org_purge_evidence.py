"""Retain purge run and bounded-batch evidence beyond organization deletion.

Revision ID: 0016_org_purge_evidence
Revises: 0015_subject_request_state
"""

from __future__ import annotations

from alembic import op

revision = "0016_org_purge_evidence"
down_revision = "0015_subject_request_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        create table org_purge_run (
            id uuid primary key,
            org_id uuid not null,
            service_term_id uuid,
            offboarding_id uuid not null unique,
            initiating_operator_id uuid references ops_account(id),
            offboarding_started_at timestamptz not null,
            purge_eligible_at timestamptz not null,
            retention_policy_reference text not null,
            status text not null default 'pending' check (status in
                ('pending','running','paused_restriction','retry_pending',
                 'needs_attention','completed')),
            execution_epoch bigint not null default 0 check (execution_epoch >= 0),
            purge_started_at timestamptz,
            completed_at timestamptz,
            deletion_rule_version text,
            inventory_manifest_key text,
            inventory_manifest_digest text,
            verification_summary jsonb not null default '{}',
            last_failure_class text,
            created_at timestamptz not null default now(),
            updated_at timestamptz not null default now(),
            check ((status='completed' and completed_at is not null) or
                   (status<>'completed' and completed_at is null))
        )
    """)
    op.execute("""
        create table org_purge_step (
            id uuid primary key,
            purge_run_id uuid not null references org_purge_run(id),
            step_key text not null,
            batch_key text not null,
            batch_manifest_key text not null,
            batch_manifest_digest text not null,
            status text not null check (status in ('authorized','completed','failed')),
            execution_epoch bigint not null check (execution_epoch >= 0),
            authorized_at timestamptz not null,
            completed_at timestamptz,
            deleted_count bigint not null default 0 check (deleted_count >= 0),
            failure_class text,
            unique (purge_run_id,step_key,batch_key),
            check ((status='completed' and completed_at is not null and failure_class is null)
                or (status='failed' and failure_class is not null)
                or (status='authorized' and completed_at is null and failure_class is null))
        )
    """)


def downgrade() -> None:
    """BlueLab migrations are forward-only."""
    raise NotImplementedError("BlueLab migrations are forward-only")
