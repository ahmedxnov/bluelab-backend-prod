-- Narrow SECURITY DEFINER helpers for unauthenticated password reset.
-- Each helper checks the request scope explicitly, owns an empty search path,
-- and exposes only the minimum projection or mutation needed by FR-IDA-006.

create or replace function app_account_for_password_reset(p_email text)
returns table(account_id uuid, org_id uuid)
language sql stable security definer set search_path = '' as $$
    select a.id, a.org_id
      from public.account a
     where nullif(pg_catalog.current_setting('app.principal_kind', true), '') = 'anonymous'
       and a.email = pg_catalog.lower(p_email)
       and a.status = 'active'
$$;

create or replace function app_issue_password_reset(
    p_account_id uuid,
    p_org_id uuid,
    p_token_id uuid,
    p_token_hash text,
    p_expires_at timestamptz,
    p_email_send_id uuid,
    p_ciphertext bytea
) returns boolean
language plpgsql volatile security definer set search_path = '' as $$
begin
    if nullif(pg_catalog.current_setting('app.principal_kind', true), '') <> 'anonymous' then
        raise exception 'app_issue_password_reset: anonymous scope required'
            using errcode = 'insufficient_privilege';
    end if;

    perform 1
      from public.account a
     where a.id = p_account_id
       and a.org_id = p_org_id
       and a.status = 'active'
     for update;
    if not found then
        return false;
    end if;

    insert into public.password_reset_token (
        id, account_id, token_hash, expires_at
    ) values (
        p_token_id, p_account_id, p_token_hash, p_expires_at
    );

    insert into public.email_send (
        id, org_id, kind, dedupe_key, account_id
    ) values (
        p_email_send_id, p_org_id, 'E1_credentials', p_token_id::text, p_account_id
    );

    insert into public.email_delivery_secret (
        email_send_id, org_id, purpose, ciphertext, expires_at
    ) values (
        p_email_send_id, p_org_id, 'password_reset_token', p_ciphertext, p_expires_at
    );

    return true;
end;
$$;

create or replace function app_consume_password_reset(
    p_token_hash text,
    p_password_hash text
) returns uuid
language plpgsql volatile security definer set search_path = '' as $$
declare
    v_account_id uuid;
begin
    if nullif(pg_catalog.current_setting('app.principal_kind', true), '') <> 'anonymous' then
        raise exception 'app_consume_password_reset: anonymous scope required'
            using errcode = 'insufficient_privilege';
    end if;

    select t.account_id
      into v_account_id
      from public.password_reset_token t
      join public.account a on a.id = t.account_id
     where t.token_hash = p_token_hash
       and t.used_at is null
       and t.expires_at > pg_catalog.now()
       and a.status = 'active'
     for update of t, a;

    if v_account_id is null then
        return null;
    end if;

    update public.password_reset_token
       set used_at = pg_catalog.now()
     where token_hash = p_token_hash
       and used_at is null;

    update public.account
       set password_hash = p_password_hash,
           updated_at = pg_catalog.now()
     where id = v_account_id
       and status = 'active';

    return v_account_id;
end;
$$;

revoke execute on function
    app_account_for_password_reset(text),
    app_issue_password_reset(uuid, uuid, uuid, text, timestamptz, uuid, bytea),
    app_consume_password_reset(text, text)
from public;

grant execute on function
    app_account_for_password_reset(text),
    app_issue_password_reset(uuid, uuid, uuid, text, timestamptz, uuid, bytea),
    app_consume_password_reset(text, text)
to bluelab_app;
