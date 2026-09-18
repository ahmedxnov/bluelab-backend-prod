-- A bounded, independently authorized purge step is the only organization
-- deletion path with freeze-guard authority (data/03 §6, data/02 T-12).

create or replace function app_due_org_purges(p_at timestamptz, p_limit integer)
returns table(org_id uuid, offboarding_id uuid)
language sql stable security definer set search_path = '' as $$
    select o.id, o.offboarding_id
      from public.org o
     where o.lifecycle_status='offboarding'
       and o.purge_eligible_at <= p_at
       and o.offboarding_id is not null
       and not exists (select 1 from public.org_deletion_restriction r
                        where r.org_id=o.id and r.status='active')
       and not exists (select 1 from public.org_lifecycle_operation p
                        where p.org_id=o.id and p.status='pending')
     order by o.purge_eligible_at, o.id
     limit least(greatest(p_limit, 0), 1000)
$$;

drop function if exists app_incomplete_org_purges(integer);
create function app_incomplete_org_purges(p_limit integer)
returns table(run_id uuid, org_id uuid, offboarding_id uuid,
              status text, retry_at timestamptz)
language sql stable security definer set search_path = '' as $$
    select r.id, r.org_id, r.offboarding_id, r.status, r.retry_at
      from public.org_purge_run r
     where r.status <> 'completed'
     order by r.updated_at, r.id
     limit least(greatest(p_limit, 0), 1000)
$$;

create or replace function app_execute_org_purge_batch(p_step_id uuid)
returns bigint language plpgsql volatile security definer
set search_path = '' as $$
declare
    v_step public.org_purge_step%rowtype;
    v_run public.org_purge_run%rowtype;
    v_org public.org%rowtype;
    v_count bigint;
    v_remaining bigint;
    v_predicate text;
    v_sql text;
begin
    select * into v_step from public.org_purge_step where id=p_step_id;
    if not found then raise exception 'purge step is not found'; end if;
    select * into v_run from public.org_purge_run where id=v_step.purge_run_id for update;
    select * into v_step from public.org_purge_step where id=p_step_id for update;
    if not found or v_step.status not in ('authorized','failed') then
        raise exception 'purge step is not authorized';
    end if;
    if not found or v_run.status not in ('running','paused_restriction','retry_pending')
       or v_run.execution_epoch <> v_step.execution_epoch then
        raise exception 'purge epoch is stale';
    end if;
    select * into v_org from public.org where id=v_run.org_id for update;
    if v_step.target_table <> 'org' and
       (not found or v_org.lifecycle_status <> 'purging'
        or v_org.offboarding_id <> v_run.offboarding_id) then
        raise exception 'purge episode is not current';
    end if;
    if v_step.target_table = 'org' and found and
       (v_org.lifecycle_status <> 'purging'
        or v_org.offboarding_id <> v_run.offboarding_id) then
        raise exception 'purge episode is not current';
    end if;
    if v_step.target_table not in (
        'email_delivery_secret','email_send','consent_record','terms_acceptance',
        'ops_fault','moment','dimension_score','scorecard','transcript_entry','attempt',
        'coach_feedback_item','badge_award','assignment_recipient','assignment',
        'candidate_token','shortlist_candidate','shortlist','candidate_report',
        'candidate','assessment_stage','hr_contact','position','product_fact',
        'fact_set','document_upload','product_document','rubric_dimension',
        'drill_concealed','drill','password_reset_token','account',
        'idempotency_record','org_service_term','org_retention_policy','org'
    ) then
        raise exception 'purge target is outside the deletion matrix';
    end if;
    if jsonb_typeof(v_step.batch_keys::jsonb) <> 'array'
       or jsonb_array_length(v_step.batch_keys::jsonb) not between 1 and 1000 then
        raise exception 'purge batch bound or shape invalid';
    end if;
    if v_step.target_table = 'email_delivery_secret' then
        v_predicate := 'exists (select 1 from public.email_send p '
            || 'where p.id=t.email_send_id and p.org_id=$1)';
    elsif v_step.target_table = 'password_reset_token' then
        v_predicate := 'exists (select 1 from public.account p '
            || 'where p.id=t.account_id and p.org_id=$1)';
    elsif v_step.target_table = 'org' then
        v_predicate := 't.id=$1';
    else
        v_predicate := 't.org_id=$1';
    end if;
    if v_step.target_table = 'org_service_term' then
        update public.org set current_service_term_id=null where id=v_run.org_id;
    end if;
    insert into bluelab_internal.purge_authorization(backend_pid,xact_id,purge_step_id)
    values(pg_catalog.pg_backend_pid(),pg_catalog.txid_current(),p_step_id);
    v_sql := format('delete from public.%I t where %s and exists ('
        || 'select 1 from jsonb_array_elements($2) k where to_jsonb(t) @> k)',
        v_step.target_table, v_predicate);
    execute v_sql using v_run.org_id, v_step.batch_keys::jsonb;
    get diagnostics v_count = row_count;
    v_sql := format('select count(*) from public.%I t where %s and exists ('
        || 'select 1 from jsonb_array_elements($2) k where to_jsonb(t) @> k)',
        v_step.target_table, v_predicate);
    execute v_sql into v_remaining using v_run.org_id, v_step.batch_keys::jsonb;
    delete from bluelab_internal.purge_authorization
    where backend_pid=pg_catalog.pg_backend_pid() and xact_id=pg_catalog.txid_current();
    if v_remaining <> 0 then
        raise exception 'purge batch verification failed';
    end if;
    return v_count;
end $$;

revoke all on function app_execute_org_purge_batch(uuid) from public;
revoke all on function app_execute_org_purge_batch(uuid) from bluelab_app;
grant execute on function app_execute_org_purge_batch(uuid) to bluelab_maintenance;
revoke all on function app_due_org_purges(timestamptz,integer) from public;
revoke all on function app_due_org_purges(timestamptz,integer) from bluelab_app;
grant execute on function app_due_org_purges(timestamptz,integer) to bluelab_maintenance;
revoke all on function app_incomplete_org_purges(integer) from public;
revoke all on function app_incomplete_org_purges(integer) from bluelab_app;
grant execute on function app_incomplete_org_purges(integer) to bluelab_maintenance;
