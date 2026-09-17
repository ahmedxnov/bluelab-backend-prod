-- System-scope discovery returns identifiers only; each transition rechecks under
-- its own organization scope and independently verified lifecycle head.
create or replace function app_due_org_service_terms(p_at timestamptz, p_limit integer)
returns table(org_id uuid)
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
begin
    if coalesce(nullif(pg_catalog.current_setting('app.principal_kind', true), ''), '') <> 'system'
       or coalesce(nullif(pg_catalog.current_setting('app.org_id', true), ''), '')
          <> '00000000-0000-0000-0000-000000000000' then
        raise exception 'app_due_org_service_terms: system scope required'
            using errcode = 'insufficient_privilege';
    end if;
    if p_limit < 1 or p_limit > 1000 then
        raise exception 'app_due_org_service_terms: invalid limit'
            using errcode = 'check_violation';
    end if;
    return query
        select o.id
        from public.org o
        where o.service_term_enforced
          and o.lifecycle_status = 'active'
          and o.current_service_term_id is not null
          and o.service_ends_at <= p_at
        order by o.service_ends_at, o.id
        limit p_limit;
end;
$$;

revoke all on function app_due_org_service_terms(timestamptz, integer) from public;
grant execute on function app_due_org_service_terms(timestamptz, integer) to bluelab_app;

create or replace function app_pending_org_term_operations(p_limit integer)
returns table(org_id uuid)
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
begin
    if coalesce(nullif(pg_catalog.current_setting('app.principal_kind', true), ''), '') <> 'system'
       or coalesce(nullif(pg_catalog.current_setting('app.org_id', true), ''), '')
          <> '00000000-0000-0000-0000-000000000000' then
        raise exception 'app_pending_org_term_operations: system scope required'
            using errcode = 'insufficient_privilege';
    end if;
    if p_limit < 1 or p_limit > 1000 then
        raise exception 'app_pending_org_term_operations: invalid limit'
            using errcode = 'check_violation';
    end if;
    return query
        select operation.org_id
        from public.org_lifecycle_operation operation
        where operation.status = 'pending'
          and operation.action in ('confirm_term','renew_term','expire_term')
        order by operation.created_at, operation.id
        limit p_limit;
end;
$$;

revoke all on function app_pending_org_term_operations(integer) from public;
grant execute on function app_pending_org_term_operations(integer) to bluelab_app;
