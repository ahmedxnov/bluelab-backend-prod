-- Narrow operations authentication, provisioning, and audit helpers.
-- Each function checks the ambient principal and exposes a fixed result shape.

create or replace function app_ops_account_for_sign_in(p_email text)
returns table(
    ops_account_id uuid,
    display_name text,
    password_hash text,
    totp_secret_ciphertext bytea,
    status text
)
language sql stable security definer set search_path = '' as $$
    select o.id, o.display_name, o.password_hash, o.totp_secret_ciphertext, o.status
      from public.ops_account o
     where nullif(pg_catalog.current_setting('app.principal_kind', true), '') = 'anonymous'
       and o.email = pg_catalog.lower(p_email)
$$;

create or replace function app_issue_initial_credentials(
    p_account_id uuid,
    p_org_id uuid,
    p_email text,
    p_display_name text,
    p_role text,
    p_manager_account_id uuid,
    p_password_hash text,
    p_email_send_id uuid,
    p_ciphertext bytea,
    p_expires_at timestamptz
) returns text
language plpgsql volatile security definer set search_path = '' as $$
declare
    v_registered_domain text;
    v_email_domain text;
    v_team_id uuid;
    v_inserted integer;
begin
    if nullif(pg_catalog.current_setting('app.principal_kind', true), '') <> 'ops'
       or not exists (
            select 1
              from public.ops_account o
             where o.id = nullif(
                       pg_catalog.current_setting('app.ops_account_id', true), ''
                   )::uuid
               and o.status = 'active'
       ) then
        raise exception 'app_issue_initial_credentials: active ops scope required'
            using errcode = 'insufficient_privilege';
    end if;

    select o.registered_domain
      into v_registered_domain
      from public.org o
     where o.id = p_org_id
     for key share;
    if not found then
        return 'org_not_found';
    end if;

    v_email_domain := pg_catalog.lower(pg_catalog.split_part(p_email, '@', 2));
    if v_email_domain = '' or v_email_domain <> v_registered_domain then
        return 'domain_mismatch';
    end if;

    if p_role = 'manager' then
        if p_manager_account_id is not null then
            return 'invalid_manager_shape';
        end if;
        v_team_id := p_account_id;
    elsif p_role = 'rep' then
        if p_manager_account_id is null or not exists (
            select 1
              from public.account a
             where a.id = p_manager_account_id
               and a.org_id = p_org_id
               and a.role = 'manager'
               and a.status = 'active'
        ) then
            return 'manager_not_found';
        end if;
        v_team_id := p_manager_account_id;
    else
        return 'invalid_role';
    end if;

    insert into public.account (
        id, org_id, team_id, email, display_name, role, password_hash,
        credential_state, status
    ) values (
        p_account_id, p_org_id, v_team_id, pg_catalog.lower(p_email),
        p_display_name, p_role, p_password_hash, 'initial', 'active'
    ) on conflict do nothing;
    get diagnostics v_inserted = row_count;
    if v_inserted = 0 then
        return 'duplicate_email';
    end if;

    insert into public.email_send (
        id, org_id, kind, dedupe_key, account_id
    ) values (
        p_email_send_id, p_org_id, 'E1_credentials', p_account_id::text, p_account_id
    );

    insert into public.email_delivery_secret (
        email_send_id, org_id, purpose, ciphertext, expires_at
    ) values (
        p_email_send_id, p_org_id, 'initial_credential', p_ciphertext, p_expires_at
    );

    return 'created';
end;
$$;

create or replace function app_append_ops_audit(
    p_id uuid,
    p_verb text,
    p_target_org_id uuid,
    p_target_ref jsonb,
    p_reason text
) returns void
language plpgsql volatile security definer set search_path = '' as $$
declare
    v_actor uuid;
begin
    if nullif(pg_catalog.current_setting('app.principal_kind', true), '') <> 'ops' then
        raise exception 'app_append_ops_audit: ops scope required'
            using errcode = 'insufficient_privilege';
    end if;
    v_actor := nullif(
        pg_catalog.current_setting('app.ops_account_id', true), ''
    )::uuid;
    if v_actor is null or not exists (
        select 1 from public.ops_account o where o.id = v_actor and o.status = 'active'
    ) then
        raise exception 'app_append_ops_audit: active ops actor required'
            using errcode = 'insufficient_privilege';
    end if;
    if pg_catalog.btrim(p_reason) = '' then
        raise exception 'app_append_ops_audit: reason required'
            using errcode = 'check_violation';
    end if;
    if p_target_ref is null or pg_catalog.jsonb_typeof(p_target_ref) <> 'object' then
        raise exception 'app_append_ops_audit: target_ref must be an object'
            using errcode = 'check_violation';
    end if;

    insert into public.ops_audit (
        id, ops_account_id, verb, target_org_id, target_ref, reason
    ) values (
        p_id, v_actor, p_verb, p_target_org_id, p_target_ref, p_reason
    );
end;
$$;

revoke execute on function
    app_ops_account_for_sign_in(text),
    app_issue_initial_credentials(uuid, uuid, text, text, text, uuid, text, uuid, bytea, timestamptz),
    app_append_ops_audit(uuid, text, uuid, jsonb, text)
from public;

grant execute on function
    app_ops_account_for_sign_in(text),
    app_issue_initial_credentials(uuid, uuid, text, text, text, uuid, text, uuid, bytea, timestamptz),
    app_append_ops_audit(uuid, text, uuid, jsonb, text)
to bluelab_app;
