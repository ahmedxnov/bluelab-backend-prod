-- T-1's scoped CAS runs under the migration-owned definer because a rep cannot
-- update assignment evidence through ordinary row policies.
create or replace function app_consume_assignment_allowance(p_drill uuid, p_rep uuid)
returns boolean
language plpgsql volatile security definer
set search_path = pg_catalog, public
as $$
declare
    v_org uuid;
    v_team uuid;
begin
    if nullif(current_setting('app.principal_kind', true), '') <> 'account'
       or nullif(current_setting('app.role', true), '') <> 'rep'
       or nullif(current_setting('app.account_id', true), '')::uuid is distinct from p_rep
    then
        return false;
    end if;
    v_org := nullif(current_setting('app.org_id', true), '')::uuid;
    v_team := nullif(current_setting('app.team_id', true), '')::uuid;
    if v_org is null or v_team is null then return false; end if;

    update public.assignment_recipient ar
       set attempts_used = ar.attempts_used + 1
      from public.assignment a
     where a.id = ar.assignment_id
       and a.drill_id = p_drill
       and a.org_id = v_org and a.team_id = v_team
       and ar.org_id = v_org and ar.team_id = v_team
       and ar.rep_account_id = p_rep
       and ar.attempts_used < a.attempts_allowed;
    return found;
end $$;

revoke all on function app_consume_assignment_allowance(uuid,uuid) from public;
grant execute on function app_consume_assignment_allowance(uuid,uuid) to bluelab_app;
