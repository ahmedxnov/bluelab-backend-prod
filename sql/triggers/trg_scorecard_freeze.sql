-- trg_scorecard_freeze — the graded record is insert-only.
--
-- Covers scorecard, dimension_score, moment and transcript_entry: any UPDATE or
-- DELETE after insert is refused (FR-SCR-003, AC-SCR-002). Dimensions and
-- moments may be inserted only while the parent attempt is in the grade-once
-- transaction; once it is graded, even appending a child is refused. A
-- scorecard that can be extended after delivery is not immutable evidence.
--
-- Plus one ordering rule that is easy to miss and load-bearing: a
-- transcript_entry INSERT is refused once its attempt has left 'in_progress'.
-- That is what forces T-2 to insert the transcript BEFORE flipping the status
-- (data/02 §1), which in turn is why the transcript can cross the call-plane
-- seam exactly once, inside one transaction (ADR-0071 rule 3).
--
-- Erasure nulls the text fields on scorecard/dimension_score and deletes moment
-- rows outright — a moment without its quote is not evidence of anything
-- (ADR-0033).
--
-- Re-applied idempotently at every release; NOT in the Alembic lineage
-- (data/04 §3). Every guard has an L3 attack test that TRIES to breach it and
-- asserts refusal — the SQL equivalent of a surviving-mutant check
-- (quality/01 §4), because a trigger nobody attacks is a trigger nobody knows
-- works.
--
-- Erasure is the ONE sanctioned writer through these guards, recognised via
-- app_in_erasure_context() (ADR-0033).

create or replace function fn_scorecard_freeze() returns trigger
language plpgsql as $$
declare
    attempt_status text;
    artifact_attempt_id uuid;
begin
    if app_in_erasure_context() or app_in_purge_context() then
        return coalesce(new, old);
    end if;

    if tg_op = 'INSERT' then
        if tg_table_name = 'transcript_entry' then
            -- The capture crosses into the backend before the T-2 status flip.
            select a.status into attempt_status from attempt a where a.id = new.attempt_id;
            if attempt_status is distinct from 'in_progress' then
                raise exception
                    'attempt % has left in_progress (%) — the transcript must be inserted before the status flip (T-2)',
                    new.attempt_id, attempt_status using errcode = 'raise_exception';
            end if;
        elsif tg_table_name in ('dimension_score', 'moment') then
            -- T-3 writes children before its final attempt.status='graded'.
            -- Looking through scorecard prevents a moment from borrowing a
            -- transcript anchor from another attempt in the same scope.
            select s.attempt_id, a.status
              into artifact_attempt_id, attempt_status
              from scorecard s
              join attempt a on a.id = s.attempt_id
             where s.id = new.scorecard_id;
            if artifact_attempt_id is null then
                raise exception 'scorecard % does not exist', new.scorecard_id
                    using errcode = 'foreign_key_violation';
            end if;
            if tg_table_name = 'moment'
               and (to_jsonb(new)->>'attempt_id')::uuid
                   is distinct from artifact_attempt_id then
                raise exception
                    'moment attempt % does not match scorecard attempt %',
                    to_jsonb(new)->>'attempt_id', artifact_attempt_id
                    using errcode = 'foreign_key_violation';
            end if;
            if attempt_status not in ('completed', 'grading_pending') then
                raise exception
                    '% cannot be appended after attempt % is % — the graded record is frozen (FR-SCR-003, AC-SCR-002)',
                    tg_table_name, artifact_attempt_id, attempt_status
                    using errcode = 'raise_exception';
            end if;
        end if;
        return new;
    end if;

    if tg_op = 'UPDATE' and app_scope_only_change(to_jsonb(old), to_jsonb(new)) then
        return new;                    -- a rep's team change cascading down
    end if;

    raise exception '% is insert-only — the graded record is frozen (FR-SCR-003, AC-SCR-002)',
        tg_table_name using errcode = 'raise_exception';
end $$;

drop trigger if exists trg_scorecard_freeze on scorecard;
create trigger trg_scorecard_freeze before update or delete on scorecard
    for each row execute function fn_scorecard_freeze();

drop trigger if exists trg_dimension_score_freeze on dimension_score;
create trigger trg_dimension_score_freeze before insert or update or delete on dimension_score
    for each row execute function fn_scorecard_freeze();

drop trigger if exists trg_moment_freeze on moment;
create trigger trg_moment_freeze before insert or update or delete on moment
    for each row execute function fn_scorecard_freeze();

drop trigger if exists trg_transcript_freeze on transcript_entry;
create trigger trg_transcript_freeze before insert or update or delete on transcript_entry
    for each row execute function fn_scorecard_freeze();
