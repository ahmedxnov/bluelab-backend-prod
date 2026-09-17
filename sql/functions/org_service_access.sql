-- One scoped organization access check; the row lock protects ordinary writes
-- against a concurrent early-termination decision in T-11.
create or replace function app_org_service_access()
returns boolean
language plpgsql volatile security definer
set search_path = pg_catalog, public
as $$
declare
    v_org org%rowtype;
    v_org_id uuid;
begin
    if nullif(current_setting('app.principal_kind', true), '') not in ('account', 'candidate', 'system') then
        return false;
    end if;
    v_org_id := nullif(current_setting('app.org_id', true), '')::uuid;
    if v_org_id is null then return false; end if;
    select * into v_org from org where id = v_org_id for share;
    if not found or v_org.lifecycle_status <> 'active' then return false; end if;
    if exists (
        select 1 from org_lifecycle_operation
         where org_id = v_org_id and status = 'pending'
    ) then return false; end if;
    if not v_org.service_term_enforced then return true; end if;
    return v_org.current_service_term_id is not null
       and v_org.service_starts_at <= clock_timestamp()
       and clock_timestamp() < v_org.service_ends_at;
end $$;

revoke all on function app_org_service_access() from public;
grant execute on function app_org_service_access() to bluelab_app;
