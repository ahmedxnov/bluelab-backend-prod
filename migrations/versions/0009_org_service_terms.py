"""Organization service-term projections and lifecycle decision fences.

Revision ID: 0009_org_service_terms
Revises: 0008_phase9_lifecycle
"""

from __future__ import annotations

from alembic import op

revision = "0009_org_service_terms"
down_revision = "0008_phase9_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Expand first; legacy organizations retain their explicit migration exception."""
    op.execute("alter table org add column service_term_enforced boolean not null default false")
    op.execute("alter table org alter column service_term_enforced set default true")
    op.execute("alter table org add column service_starts_at timestamptz")
    op.execute("alter table org add column service_ends_at timestamptz")
    op.execute("alter table org add column current_service_term_id uuid")
    op.execute("alter table org add column lifecycle_status text not null default 'active'")
    op.execute("alter table org add column lifecycle_sequence bigint not null default 0")
    op.execute("alter table org add column offboarding_id uuid")
    op.execute("alter table org add column offboarding_started_at timestamptz")
    op.execute("alter table org add column offboarding_started_by uuid")
    op.execute("alter table org add column purge_eligible_at timestamptz")
    op.execute("alter table org add column retention_policy_reference text")
    op.execute("alter table org add constraint ck_org_lifecycle_status check (lifecycle_status in ('active','offboarding','purging'))")
    op.execute("alter table org add constraint ck_org_lifecycle_sequence check (lifecycle_sequence >= 0)")
    op.execute("alter table org add constraint ck_org_service_interval check ((service_starts_at is null and service_ends_at is null and current_service_term_id is null) or (service_starts_at is not null and service_ends_at is not null and current_service_term_id is not null and service_ends_at > service_starts_at))")
    op.execute("alter table org add constraint ck_org_offboarding_fields check ((lifecycle_status = 'active' and offboarding_id is null and offboarding_started_at is null and offboarding_started_by is null and purge_eligible_at is null and retention_policy_reference is null) or (lifecycle_status in ('offboarding','purging') and offboarding_id is not null and offboarding_started_at is not null and purge_eligible_at is not null and retention_policy_reference is not null and purge_eligible_at >= offboarding_started_at))")
    op.execute("create unique index uq_org_offboarding_id on org(offboarding_id) where offboarding_id is not null")
    op.execute("create index idx_org_service_due on org(service_ends_at, id) where lifecycle_status = 'active' and current_service_term_id is not null")
    op.execute("create index idx_org_offboarding_due on org(purge_eligible_at, id) where lifecycle_status = 'offboarding'")

    op.execute("""
        create table org_service_term (
            id uuid primary key, org_id uuid not null references org(id),
            lifecycle_sequence bigint not null check (lifecycle_sequence > 0),
            start_on date not null, last_access_on date not null,
            calendar_timezone text not null, starts_at timestamptz not null,
            ends_at timestamptz not null, contract_reference text not null,
            retention_policy_reference text not null, confirmed_by uuid not null,
            confirmed_at timestamptz not null, reason text not null,
            constraint uq_org_service_term_org_sequence unique (org_id, lifecycle_sequence),
            constraint uq_org_service_term_org_id unique (org_id, id),
            constraint ck_org_service_term_valid_term check (last_access_on >= start_on and ends_at > starts_at)
        )
    """)
    op.execute("alter table org add constraint fk_org_current_service_term foreign key (id, current_service_term_id) references org_service_term(org_id, id)")
    op.execute("""
        create table org_retention_policy (
            org_id uuid primary key references org(id),
            policy_reference text not null, contract_reference text not null,
            period_value integer not null, period_unit text not null,
            calendar_timezone text, effective_sequence bigint not null,
            approved_at timestamptz not null, approved_by uuid not null,
            reason text not null,
            constraint ck_org_retention_policy_valid_policy_values check (period_value > 0 and effective_sequence > 0),
            constraint ck_org_retention_policy_period_unit_valid check (period_unit in ('elapsed_days','calendar_days','calendar_months')),
            constraint ck_org_retention_policy_policy_timezone check ((period_unit = 'elapsed_days' and calendar_timezone is null) or (period_unit in ('calendar_days','calendar_months') and calendar_timezone is not null))
        )
    """)
    op.execute("""
        create table org_lifecycle_operation (
            id uuid primary key, org_id uuid not null,
            service_term_id uuid, offboarding_id uuid,
            action text not null, expected_sequence bigint not null check (expected_sequence >= 0),
            status text not null default 'pending',
            actor_ops_account_id uuid references ops_account(id), reason text not null,
            retention_policy_reference text, requested_deadline timestamptz,
            restriction_id uuid, resulting_sequence bigint,
            created_at timestamptz not null default now(), resolved_at timestamptz,
            constraint ck_org_lifecycle_operation_status_valid check (status in ('pending','applied','rejected')),
            constraint ck_org_lifecycle_operation_action_valid check (action in ('confirm_term','renew_term','expire_term','start','cancel','extend','policy_revision','create_restriction','release_restriction','claim','authorize_destructive_step','complete','record_post_completion_restriction')),
            constraint ck_org_lifecycle_operation_result check ((status = 'pending' and resolved_at is null and resulting_sequence is null) or (status = 'rejected' and resolved_at is not null and resulting_sequence is null) or (status = 'applied' and resolved_at is not null and resulting_sequence is not null))
        )
    """)
    op.execute("create unique index uq_org_lifecycle_pending on org_lifecycle_operation(org_id) where status = 'pending'")
    op.execute("alter table ops_audit drop constraint fk_ops_audit_target_org_id")
    op.execute("alter table ops_audit drop constraint ck_ops_audit_verb_valid")
    op.execute("alter table ops_audit add constraint ck_ops_audit_verb_valid check (verb in ('provision_org','confirm_org_term','renew_org_term','provision_account','deactivate_account','change_team_mapping','transfer_position','resolve_fault','execute_erasure','execute_export','resolve_subject_request','start_org_offboarding','cancel_org_offboarding','extend_org_deadline','create_org_deletion_restriction','release_org_deletion_restriction','record_post_completion_restriction','revise_org_retention_policy','inspect_org_purge_evidence','resolve_org_purge_fault'))")


def downgrade() -> None:
    raise NotImplementedError("BlueLab migrations are forward-only")
