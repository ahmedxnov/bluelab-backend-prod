-- Identity-only playback fault re-drive. The ops caller can update only the
-- recording availability state named by an open playback fault; it receives no
-- recording key or customer content.

create or replace function app_redrive_playback_fault(
    p_fault_id uuid,
    p_available boolean
) returns text
language plpgsql volatile security definer set search_path = '' as $$
declare
    v_attempt_id uuid;
begin
    if nullif(pg_catalog.current_setting('app.principal_kind', true), '') <> 'ops'
       or not exists (
            select 1 from public.ops_account o
             where o.id = nullif(
                 pg_catalog.current_setting('app.ops_account_id', true), ''
             )::uuid
               and o.status = 'active'
       ) then
        raise exception 'app_redrive_playback_fault: active ops scope required'
            using errcode = 'insufficient_privilege';
    end if;

    select f.attempt_id into v_attempt_id
      from public.ops_fault f
     where f.id = p_fault_id
       and f.kind = 'playback_asset'
       and f.status = 'open';
    if not found then
        return 'not_open';
    end if;

    update public.attempt
       set recording_status = case when p_available then 'available' else 'unavailable' end
     where id = v_attempt_id;
    if not found then
        return 'not_found';
    end if;
    return 'updated';
end;
$$;

revoke execute on function app_redrive_playback_fault(uuid, boolean) from public;
grant execute on function app_redrive_playback_fault(uuid, boolean) to bluelab_app;
