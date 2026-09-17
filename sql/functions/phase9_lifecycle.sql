-- Phase 9 ops lifecycle verbs and T-10 erasure boundary.
-- SECURITY DEFINER is deliberate: ops has no general customer-content policy.

create or replace function app_deactivate_account(p_account_id uuid)
returns table(org_id uuid, dependent_reps bigint, open_positions bigint)
language plpgsql security definer
set search_path = pg_catalog, public
as $$
declare
    v account%rowtype;
begin
    select * into v from account where id=p_account_id for update;
    if not found then return; end if;
    select count(*) into dependent_reps from account
     where team_id=v.id and role='rep' and status='active';
    select count(*) into open_positions from position
     where team_id=v.id and status<>'closed';
    org_id := v.org_id;
    if v.role='manager' and (dependent_reps>0 or open_positions>0) then
        return next;
        return;
    end if;
    update account set status='deactivated',deactivated_at=coalesce(deactivated_at,now()),updated_at=now()
     where id=p_account_id;
    dependent_reps := 0;
    open_positions := 0;
    return next;
end $$;

create or replace function app_change_team(p_account_id uuid,p_manager_id uuid)
returns table(org_id uuid,team_id uuid)
language plpgsql security definer
set search_path = pg_catalog, public
as $$
declare
    v_org uuid;
begin
    perform pg_advisory_xact_lock(hashtextextended(p_account_id::text,0));
    select a.org_id into v_org from account a
     where a.id=p_account_id and a.role='rep' and a.status='active' for update;
    if not found then
        return;
    end if;
    perform 1 from account m where m.id=p_manager_id and m.org_id=v_org
      and m.role='manager' and m.status='active' for update;
    if not found then
        return;
    end if;
    update account set team_id=p_manager_id,updated_at=now() where id=p_account_id;
    org_id := v_org;
    team_id := p_manager_id;
    return next;
end $$;

create or replace function app_transfer_position(p_position_id uuid,p_manager_id uuid)
returns table(org_id uuid,team_id uuid)
language plpgsql security definer
set search_path = pg_catalog, public
as $$
declare
    v_org uuid;
begin
    select p.org_id into v_org from position p where p.id=p_position_id for update;
    if not found then
        return;
    end if;
    perform 1 from account m where m.id=p_manager_id and m.org_id=v_org
      and m.role='manager' and m.status='active' for update;
    if not found then
        return;
    end if;
    update position set team_id=p_manager_id,updated_at=now() where id=p_position_id;
    org_id := v_org;
    team_id := p_manager_id;
    return next;
end $$;

create or replace function app_subject_request_valid(
    p_org_id uuid,p_subject_kind text,p_subject_id uuid,p_erasure boolean
) returns boolean
language sql stable security definer
set search_path = pg_catalog, public
as $$
select case p_subject_kind
    when 'account' then exists(
        select 1 from account where id=p_subject_id and org_id=p_org_id
          and (not p_erasure or status='deactivated'))
    when 'candidate' then exists(
        select 1 from candidate where id=p_subject_id and org_id=p_org_id)
    else false end
$$;

create or replace function app_execute_erasure(p_request_id uuid)
returns jsonb
language plpgsql security definer
set search_path = pg_catalog, public
as $$
declare
    r erasure_request%rowtype;
    v_attempts bigint := 0;
    v_moments bigint := 0;
    v_transcripts bigint := 0;
    v_scores bigint := 0;
    v_dimensions bigint := 0;
    v_tokens bigint := 0;
    v_consent bigint := 0;
    v_terms bigint := 0;
    v_feedback bigint := 0;
    v_badges bigint := 0;
    v_objects jsonb := '[]'::jsonb;
    v_evidence jsonb;
begin
    select * into r from erasure_request where id=p_request_id for update;
    if not found then raise exception 'unknown erasure request'; end if;
    if r.status='executed' then return r.evidence; end if;
    perform pg_advisory_xact_lock(hashtextextended(r.subject_id::text,0));
    perform set_config('app.erasure_context','on',true);

    select coalesce(jsonb_agg(key order by key),'[]'::jsonb) into v_objects from (
        select recording_object_key key from attempt
         where recording_object_key is not null and
          ((r.subject_kind='account' and rep_account_id=r.subject_id) or
           (r.subject_kind='candidate' and candidate_id=r.subject_id))
        union
        select pdf_object_key from candidate_report
         where pdf_object_key is not null and r.subject_kind='candidate' and candidate_id=r.subject_id
        union
        select bundle_object_key from export_request
         where bundle_object_key is not null and org_id=r.org_id
           and subject_kind=r.subject_kind and subject_id=r.subject_id
    ) objects;

    delete from moment where attempt_id in (
        select id from attempt where
         (r.subject_kind='account' and rep_account_id=r.subject_id) or
         (r.subject_kind='candidate' and candidate_id=r.subject_id));
    get diagnostics v_moments=row_count;
    delete from transcript_entry where attempt_id in (
        select id from attempt where
         (r.subject_kind='account' and rep_account_id=r.subject_id) or
         (r.subject_kind='candidate' and candidate_id=r.subject_id));
    get diagnostics v_transcripts=row_count;
    update dimension_score set note=null where scorecard_id in (
        select s.id from scorecard s join attempt a on a.id=s.attempt_id where
         (r.subject_kind='account' and a.rep_account_id=r.subject_id) or
         (r.subject_kind='candidate' and a.candidate_id=r.subject_id));
    get diagnostics v_dimensions=row_count;
    update scorecard set takeaway=null where attempt_id in (
        select id from attempt where
         (r.subject_kind='account' and rep_account_id=r.subject_id) or
         (r.subject_kind='candidate' and candidate_id=r.subject_id));
    get diagnostics v_scores=row_count;
    update attempt set recording_object_key=null,recording_status='erased' where
        (r.subject_kind='account' and rep_account_id=r.subject_id) or
        (r.subject_kind='candidate' and candidate_id=r.subject_id);
    get diagnostics v_attempts=row_count;

    if r.subject_kind='account' then
        update account set email='erased+'||id||'@erased.invalid',display_name='Erased person',
            password_hash=null,updated_at=now() where id=r.subject_id and org_id=r.org_id;
        delete from password_reset_token where account_id=r.subject_id;
        get diagnostics v_tokens=row_count;
        delete from coach_feedback_item where rep_account_id=r.subject_id;
        get diagnostics v_feedback=row_count;
        delete from badge_award where account_id=r.subject_id;
        get diagnostics v_badges=row_count;
    else
        update candidate set name='Erased person',email='erased+'||id||'@erased.invalid',
            phone=null,linkedin=null,source=null,internal_note=null,preflight=null,updated_at=now()
         where id=r.subject_id and org_id=r.org_id;
        delete from candidate_token where candidate_id=r.subject_id;
        get diagnostics v_tokens=row_count;
        update candidate_report set takeaway=null,pdf_object_key=null,pdf_status='erased'
         where candidate_id=r.subject_id;
    end if;
    delete from consent_record where
        (r.subject_kind='account' and account_id=r.subject_id) or
        (r.subject_kind='candidate' and candidate_id=r.subject_id);
    get diagnostics v_consent=row_count;
    delete from terms_acceptance where
        (r.subject_kind='account' and account_id=r.subject_id) or
        (r.subject_kind='candidate' and candidate_id=r.subject_id);
    get diagnostics v_terms=row_count;
    delete from email_delivery_secret where email_send_id in (
        select id from email_send where
         (r.subject_kind='account' and account_id=r.subject_id) or
         (r.subject_kind='candidate' and candidate_id=r.subject_id));
    update export_request set status='failed',bundle_object_key=null,expires_at=null
     where org_id=r.org_id and subject_kind=r.subject_kind and subject_id=r.subject_id;

    v_evidence := jsonb_build_object(
        'attempts_preserved',v_attempts,'moments_deleted',v_moments,
        'transcripts_deleted',v_transcripts,'scorecards_redacted',v_scores,
        'dimension_scores_redacted',v_dimensions,'tokens_deleted',v_tokens,
        'consent_deleted',v_consent,'terms_deleted',v_terms,
        'feedback_deleted',v_feedback,'badges_deleted',v_badges,
        'object_keys',v_objects);
    return v_evidence;
end $$;

create or replace function app_retention_sweep(p_class text,p_at timestamptz)
returns jsonb
language plpgsql security definer
set search_path = pg_catalog, public
as $$
declare
    v_first bigint := 0;
    v_second bigint := 0;
    v_objects jsonb := '[]'::jsonb;
begin
    if session_user <> 'bluelab' and (
        coalesce(nullif(pg_catalog.current_setting('app.principal_kind', true), ''), '') <> 'system'
        or coalesce(nullif(pg_catalog.current_setting('app.org_id', true), ''), '')
           <> '00000000-0000-0000-0000-000000000000'
    ) then
        raise exception 'app_retention_sweep: maintenance scope required'
            using errcode = 'insufficient_privilege';
    end if;
    if p_at > pg_catalog.clock_timestamp() + interval '1 minute' then
        raise exception 'app_retention_sweep: future cutoff refused'
            using errcode = 'check_violation';
    end if;
    case p_class
    when 'RC-3' then
        delete from candidate_token
         where coalesce(revoked_at,expires_at) <= p_at-interval '90 days';
        get diagnostics v_first=row_count;
        delete from password_reset_token
         where coalesce(used_at,expires_at) <= p_at-interval '7 days';
        get diagnostics v_second=row_count;
    when 'RC-4' then
        select coalesce(jsonb_agg(object_key order by object_key),'[]'::jsonb)
          into v_objects from document_upload
         where status in ('extracted','failed') and created_at<=p_at-interval '30 days';
        delete from document_upload
         where status in ('extracted','failed') and created_at<=p_at-interval '30 days';
        get diagnostics v_first=row_count;
        delete from fact_set where kind='draft' and created_at<=p_at-interval '90 days';
        get diagnostics v_second=row_count;
    when 'RC-5' then
        delete from email_send where created_at<=p_at-interval '12 months';
        get diagnostics v_first=row_count;
    when 'RC-6' then
        delete from coach_feedback_item where created_at<=p_at-interval '12 months';
        get diagnostics v_first=row_count;
    when 'RC-8' then
        select coalesce(jsonb_agg(bundle_object_key order by bundle_object_key),'[]'::jsonb)
          into v_objects from export_request
         where expires_at<=p_at and bundle_object_key is not null;
        update export_request set bundle_object_key=null
         where expires_at<=p_at and bundle_object_key is not null;
        get diagnostics v_first=row_count;
    when 'RC-9' then
        delete from email_delivery_secret where expires_at<=p_at;
        get diagnostics v_first=row_count;
    else
        raise exception 'unknown retention class';
    end case;
    return jsonb_build_object(
        'class',p_class,'primary_count',v_first,'secondary_count',v_second,
        'object_keys',v_objects);
end $$;

create or replace function app_stale_pending_recordings(p_at timestamptz)
returns jsonb
language plpgsql security definer
set search_path = pg_catalog, public
as $$
declare
    v_result jsonb;
begin
    if session_user <> 'bluelab' and (
        coalesce(nullif(pg_catalog.current_setting('app.principal_kind', true), ''), '') <> 'system'
        or coalesce(nullif(pg_catalog.current_setting('app.org_id', true), ''), '')
           <> '00000000-0000-0000-0000-000000000000'
    ) then
        raise exception 'app_stale_pending_recordings: maintenance scope required'
            using errcode = 'insufficient_privilege';
    end if;
    if p_at > pg_catalog.clock_timestamp() + interval '1 minute' then
        raise exception 'app_stale_pending_recordings: future cutoff refused'
            using errcode = 'check_violation';
    end if;
    with stale as (
    select id from attempt
     where recording_status='pending'
       and ended_at<=p_at-interval '10 minutes'
     order by ended_at,id
     limit 500
     for update skip locked
), updated as (
    update attempt a set recording_status='unavailable'
      from stale s where a.id=s.id and a.recording_status='pending'
    returning a.id,a.org_id
), faults as (
    insert into ops_fault(id,org_id,kind,attempt_id,detail)
    select gen_random_uuid(),u.org_id,'playback_asset',u.id,'{}'::jsonb
      from updated u
     where not exists (
         select 1 from ops_fault f where f.attempt_id=u.id
           and f.kind='playback_asset' and f.status='open'
     )
    returning id
)
select jsonb_build_object(
    'attempts_unavailable',(select count(*) from updated),
    'faults_opened',(select count(*) from faults)
) into v_result;
return v_result;
end;
$$;

create or replace function app_verify_erasure(p_request_id uuid)
returns jsonb
language sql stable security definer
set search_path = pg_catalog, public
as $$
with r as (
    select * from erasure_request where id=p_request_id
), subject_attempts as (
    select a.id,a.recording_object_key from attempt a,r where
      (r.subject_kind='account' and a.rep_account_id=r.subject_id) or
      (r.subject_kind='candidate' and a.candidate_id=r.subject_id)
)
select jsonb_build_object(
    'transcripts_remaining',(select count(*) from transcript_entry where attempt_id in(select id from subject_attempts)),
    'moments_remaining',(select count(*) from moment where attempt_id in(select id from subject_attempts)),
    'recording_refs_remaining',(select count(*) from subject_attempts where recording_object_key is not null),
    'scorecard_text_remaining',(select count(*) from scorecard where attempt_id in(select id from subject_attempts) and takeaway is not null),
    'dimension_text_remaining',(select count(*) from dimension_score where scorecard_id in(select id from scorecard where attempt_id in(select id from subject_attempts)) and note is not null),
    'tokens_remaining',(select count(*) from r where
      (r.subject_kind='account' and exists(select 1 from password_reset_token t where t.account_id=r.subject_id)) or
      (r.subject_kind='candidate' and exists(select 1 from candidate_token t where t.candidate_id=r.subject_id))),
    'consent_remaining',(select count(*) from consent_record c,r where (r.subject_kind='account' and c.account_id=r.subject_id) or (r.subject_kind='candidate' and c.candidate_id=r.subject_id)),
    'terms_remaining',(select count(*) from terms_acceptance t,r where (r.subject_kind='account' and t.account_id=r.subject_id) or (r.subject_kind='candidate' and t.candidate_id=r.subject_id)),
    'feedback_remaining',(select count(*) from coach_feedback_item f,r where r.subject_kind='account' and f.rep_account_id=r.subject_id),
    'badges_remaining',(select count(*) from badge_award b,r where r.subject_kind='account' and b.account_id=r.subject_id),
    'report_text_remaining',(select count(*) from candidate_report p,r where r.subject_kind='candidate' and p.candidate_id=r.subject_id and (p.takeaway is not null or p.pdf_object_key is not null)),
    'delivery_secrets_remaining',(select count(*) from email_delivery_secret s join email_send e on e.id=s.email_send_id,r where (r.subject_kind='account' and e.account_id=r.subject_id) or (r.subject_kind='candidate' and e.candidate_id=r.subject_id)),
    'export_objects_remaining',(select count(*) from export_request e,r where e.org_id=r.org_id and e.subject_kind=r.subject_kind and e.subject_id=r.subject_id and e.bundle_object_key is not null),
    'live_pii_remaining',(select count(*) from r where
      (r.subject_kind='account' and exists(select 1 from account a where a.id=r.subject_id and (a.email<>'erased+'||a.id||'@erased.invalid' or a.display_name<>'Erased person' or a.password_hash is not null))) or
      (r.subject_kind='candidate' and exists(select 1 from candidate c where c.id=r.subject_id and (c.email<>'erased+'||c.id||'@erased.invalid' or c.name<>'Erased person' or c.phone is not null or c.linkedin is not null or c.source is not null or c.internal_note is not null or c.preflight is not null))))
)
$$;

create or replace function app_restore_erasure_marker(
    p_request_id uuid,p_org_id uuid,p_subject_kind text,p_subject_id uuid,
    p_requested_at timestamptz,p_executed_by uuid
) returns void
language plpgsql security definer
set search_path = pg_catalog, public
as $$
begin
    insert into erasure_request(
        id,org_id,subject_kind,subject_id,status,requested_at,executed_by,evidence)
    values(
        p_request_id,p_org_id,p_subject_kind,p_subject_id,'pending',p_requested_at,
        p_executed_by,'{}'::jsonb)
    on conflict(id) do update set
        status=case when erasure_request.status='executed' then 'executed' else 'pending' end,
        org_id=excluded.org_id,subject_kind=excluded.subject_kind,
        subject_id=excluded.subject_id,requested_at=excluded.requested_at,
        executed_by=excluded.executed_by;
end $$;

create or replace function app_object_inventory()
returns jsonb
language sql stable security definer
set search_path = pg_catalog, public
as $$
select coalesce(jsonb_agg(key order by key),'[]'::jsonb) from (
    select recording_object_key key from attempt where recording_object_key is not null
    union select object_key from document_upload
    union select pdf_object_key from candidate_report where pdf_object_key is not null
    union select bundle_object_key from export_request where bundle_object_key is not null
) inventory
$$;

create or replace function app_mark_missing_object(p_key text)
returns text
language plpgsql security definer
set search_path = pg_catalog, public
as $$
declare
    v_count bigint;
begin
    update attempt set recording_object_key=null,recording_status='unavailable'
     where recording_object_key=p_key;
    get diagnostics v_count=row_count;
    if v_count>0 then return 'recording'; end if;
    update candidate_report set pdf_object_key=null,pdf_status='failed'
     where pdf_object_key=p_key;
    get diagnostics v_count=row_count;
    if v_count>0 then return 'report'; end if;
    delete from document_upload where object_key=p_key;
    get diagnostics v_count=row_count;
    if v_count>0 then return 'upload'; end if;
    update export_request set bundle_object_key=null,status='failed'
     where bundle_object_key=p_key;
    get diagnostics v_count=row_count;
    if v_count>0 then return 'export'; end if;
    return 'none';
end $$;

revoke all on function app_deactivate_account(uuid) from public;
revoke all on function app_change_team(uuid,uuid) from public;
revoke all on function app_transfer_position(uuid,uuid) from public;
revoke all on function app_subject_request_valid(uuid,text,uuid,boolean) from public;
revoke all on function app_execute_erasure(uuid) from public;
revoke all on function app_retention_sweep(text,timestamptz) from public;
revoke all on function app_stale_pending_recordings(timestamptz) from public;
revoke all on function app_verify_erasure(uuid) from public;
revoke all on function app_restore_erasure_marker(uuid,uuid,text,uuid,timestamptz,uuid) from public;
revoke all on function app_object_inventory() from public;
revoke all on function app_mark_missing_object(text) from public;
grant execute on function app_deactivate_account(uuid) to bluelab_app;
grant execute on function app_change_team(uuid,uuid) to bluelab_app;
grant execute on function app_transfer_position(uuid,uuid) to bluelab_app;
grant execute on function app_subject_request_valid(uuid,text,uuid,boolean) to bluelab_app;
grant execute on function app_execute_erasure(uuid) to bluelab_app;
grant execute on function app_retention_sweep(text,timestamptz) to bluelab_app;
grant execute on function app_stale_pending_recordings(timestamptz) to bluelab_app;
grant execute on function app_verify_erasure(uuid) to bluelab_app;
grant execute on function app_restore_erasure_marker(uuid,uuid,text,uuid,timestamptz,uuid) to bluelab_app;
grant execute on function app_object_inventory() to bluelab_app;
grant execute on function app_mark_missing_object(text) to bluelab_app;
