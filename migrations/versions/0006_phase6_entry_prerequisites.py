"""Phase 6 request replay and candidate-token resolution primitives.

Revision ID: 0006_phase6_entry_prerequisites
Revises: 0005_phase3_frozen_content
Created: 2026-09-15
"""

from __future__ import annotations

from alembic import op

revision: str = "0006_phase6_entry_prerequisites"
down_revision: str | None = "0005_phase3_frozen_content"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the minimal durable prerequisites used by Phase 6 writes."""
    op.execute(
        """
create table idempotency_record (
    org_id uuid not null references org(id),
    owner_account_id uuid not null references account(id),
    endpoint text not null,
    key text not null,
    body_hash text not null,
    response_status smallint not null check (response_status between 200 and 299),
    response_body jsonb not null,
    expires_at timestamptz not null,
    created_at timestamptz not null default now(),
    primary key (org_id, owner_account_id, endpoint, key),
    constraint ck_idempotency_record_expiry check (expires_at > created_at)
)
        """
    )
    op.execute("alter table email_delivery_secret drop constraint ck_email_delivery_secret_purpose_valid")
    op.execute("""alter table email_delivery_secret add constraint ck_email_delivery_secret_purpose_valid
        check (purpose in ('initial_credential','password_reset_token','candidate_invite_token'))""")
    op.execute(
        "create index idx_idempotency_record_expiry on idempotency_record (expires_at)"
    )
    op.execute(
        """
create or replace function app_candidate_token_binding(p_token_hash text)
returns table(
    token_hash text,
    org_id uuid,
    team_id uuid,
    position_id uuid,
    candidate_id uuid,
    expires_at timestamptz,
    revoked boolean
)
language sql stable security definer set search_path = '' as $$
    select t.token_hash, t.org_id, t.team_id, c.position_id, t.candidate_id,
           t.expires_at, t.revoked_at is not null
      from public.candidate_token t
      join public.candidate c
        on c.id = t.candidate_id
       and c.org_id = t.org_id
       and c.team_id = t.team_id
     where nullif(pg_catalog.current_setting('app.principal_kind', true), '') = 'anonymous'
       and t.token_hash = p_token_hash
$$
        """
    )
    op.execute(
        "revoke execute on function app_candidate_token_binding(text) from public"
    )
    op.execute(
        "grant execute on function app_candidate_token_binding(text) to bluelab_app"
    )
    op.execute(
        """
create or replace function app_record_candidate_preflight(p_consent_id uuid, p_terms_id uuid, p_preflight jsonb)
returns void language plpgsql security definer set search_path = '' as $$
declare v_org uuid := nullif(pg_catalog.current_setting('app.org_id',true),'')::uuid;
        v_candidate uuid := nullif(pg_catalog.current_setting('app.candidate_id',true),'')::uuid;
        v_position uuid := nullif(pg_catalog.current_setting('app.position_id',true),'')::uuid;
        v_notice text; v_terms text; v_privacy text;
begin
 if nullif(pg_catalog.current_setting('app.principal_kind',true),'') <> 'candidate' then raise exception 'candidate scope required'; end if;
 if not exists (select 1 from public.candidate where id=v_candidate and org_id=v_org and position_id=v_position and completed_at is null) then raise exception 'candidate unavailable'; end if;
 select version into v_notice from public.legal_document_version where kind='recording_consent_notice' and effective_at<=now() order by effective_at desc,id desc limit 1;
 select version into v_terms from public.legal_document_version where kind='terms_of_use' and effective_at<=now() order by effective_at desc,id desc limit 1;
 select version into v_privacy from public.legal_document_version where kind='privacy_notice' and effective_at<=now() order by effective_at desc,id desc limit 1;
 if v_notice is null or v_terms is null or v_privacy is null then raise exception 'current legal documents unavailable'; end if;
 insert into public.consent_record(id,org_id,candidate_id,notice_version) values(p_consent_id,v_org,v_candidate,v_notice) on conflict(candidate_id,notice_version) do nothing;
 insert into public.terms_acceptance(id,org_id,candidate_id,terms_version,privacy_version) values(p_terms_id,v_org,v_candidate,v_terms,v_privacy) on conflict(candidate_id,terms_version,privacy_version) do nothing;
 update public.candidate set preflight=p_preflight,updated_at=now() where id=v_candidate and org_id=v_org and position_id=v_position;
end $$
        """
    )
    op.execute("revoke execute on function app_record_candidate_preflight(uuid,uuid,jsonb) from public")
    op.execute("grant execute on function app_record_candidate_preflight(uuid,uuid,jsonb) to bluelab_app")
    op.execute("""create or replace function app_candidate_org_name() returns text language sql stable security definer set search_path = '' as $$
      select o.name from public.org o where nullif(pg_catalog.current_setting('app.principal_kind',true),'')='candidate'
        and o.id=nullif(pg_catalog.current_setting('app.org_id',true),'')::uuid $$""")
    op.execute("revoke execute on function app_candidate_org_name() from public")
    op.execute("grant execute on function app_candidate_org_name() to bluelab_app")


def downgrade() -> None:
    """BlueLab migrations are forward-only."""
    raise NotImplementedError("BlueLab migrations are forward-only")
