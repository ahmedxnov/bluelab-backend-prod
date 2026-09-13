"""Phase 2 identity, operations, and E-1 delivery storage.

Revision ID: 0004_phase2_identity_services
Revises: 0003_platform_reference_seed
Created: 2026-09-11
"""

from __future__ import annotations

from alembic import op

revision: str = "0004_phase2_identity_services"
down_revision: str | None = "0003_platform_reference_seed"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the Phase 2 columns and short-lived delivery-secret table."""
    # Phase 2 narrows operations verbs and makes the audit trail append-only.
    # Remove the superseded generated names before the current policy set is
    # applied; otherwise PostgreSQL retains those broader grants as extras.
    op.execute("drop policy if exists org_ops_verbs on org")
    op.execute("drop policy if exists account_ops_verbs on account")
    op.execute("drop policy if exists ops_audit_ops_all on ops_audit")
    op.execute("drop policy if exists ops_audit_system_update on ops_audit")

    op.execute("alter table legal_document_version add column url text")
    op.execute(
        """
update legal_document_version
   set url = '/legal/' || replace(kind, '_', '-') || '?version=' || version
 where url is null
        """
    )
    op.execute("alter table legal_document_version alter column url set not null")

    op.execute("alter table org add column registered_domain text")
    op.execute(
        """
do $$
begin
    if exists (
        select o.id
          from org o
          left join account a on a.org_id = o.id
         group by o.id
        having count(distinct split_part(lower(a.email), '@', 2)) <> 1
    ) then
        raise exception 'cannot infer one registered domain for every existing org';
    end if;
end
$$
        """
    )
    op.execute(
        """
update org o
   set registered_domain = inferred.domain
  from (
        select org_id, min(split_part(lower(email), '@', 2)) as domain
          from account
         group by org_id
       ) inferred
 where inferred.org_id = o.id
        """
    )
    op.execute("alter table org alter column registered_domain set not null")
    op.execute(
        "alter table org add constraint ck_org_registered_domain_lower "
        "check (registered_domain = lower(registered_domain))"
    )

    op.execute("alter table ops_account add column totp_secret_ciphertext bytea")
    # Existing pre-Phase-2 operator rows receive an invalid, fail-closed envelope;
    # they cannot authenticate until an operator rotates a real TOTP secret.
    op.execute("update ops_account set totp_secret_ciphertext = ''::bytea")
    op.execute("alter table ops_account alter column totp_secret_ciphertext set not null")

    op.execute(
        "alter table email_send add constraint uq_email_send_id_org_id unique (id, org_id)"
    )
    op.execute(
        """
create table email_delivery_secret (
    email_send_id uuid not null,
    org_id uuid not null,
    purpose text not null,
    ciphertext bytea not null,
    expires_at timestamptz not null,
    created_at timestamptz not null default now(),
    constraint pk_email_delivery_secret primary key (email_send_id),
    constraint fk_email_delivery_secret_org_id foreign key (org_id) references org (id),
    constraint fk_email_delivery_secret_email_send_id_org_id
        foreign key (email_send_id, org_id)
        references email_send (id, org_id) on delete cascade,
    constraint ck_email_delivery_secret_purpose_valid
        check (purpose in ('initial_credential', 'password_reset_token')),
    constraint ck_email_delivery_secret_expires_after_creation
        check (expires_at > created_at)
)
        """
    )
    op.execute("create index ix_email_delivery_secret_org_id on email_delivery_secret (org_id)")

    # The app and work planes execute Procrastinate's pinned invoker-rights SQL.
    # Limit sequence use to the identities those paths actually allocate.
    op.execute(
        "grant usage, select on sequence "
        "procrastinate_jobs_id_seq, procrastinate_events_id_seq, "
        "procrastinate_workers_id_seq to bluelab_app"
    )


def downgrade() -> None:
    """The project uses forward-only migrations; downgrade is intentionally unavailable."""
    raise NotImplementedError("BlueLab migrations are forward-only")
