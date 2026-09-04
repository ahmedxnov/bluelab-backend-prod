-- trg_stage_freeze — the assessment freezes at the first invite (FR-HIR-005).
--
-- The moment one candidate has been invited, the assessment is the thing every
-- candidate on this position will be compared on. Adding, removing or reordering
-- a stage after that makes two candidates incomparable — which is the whole
-- basis of a defensible hiring decision (CMP-003).
--
-- Scope-cascade columns are exempt: a position transfer (FR-HIR-018) rewrites
-- org_id/team_id down the subtree by ON UPDATE CASCADE, and the assessment is
-- unchanged — only its owner moved.
--
-- Re-applied idempotently at every release; NOT in the Alembic lineage
-- (data/04 §3). Every guard has an L3 attack test that TRIES to breach it and
-- asserts refusal — the SQL equivalent of a surviving-mutant check
-- (quality/01 §4), because a trigger nobody attacks is a trigger nobody knows
-- works.
--
-- Erasure is the ONE sanctioned writer through these guards, recognised via
-- app_in_erasure_context() (ADR-0033).

create or replace function fn_stage_freeze() returns trigger
language plpgsql as $$
declare
    target_position uuid := coalesce(new.position_id, old.position_id);
    frozen boolean;
begin
    if app_in_erasure_context() then
        return coalesce(new, old);
    end if;
    if tg_op = 'UPDATE' and app_scope_only_change(to_jsonb(old), to_jsonb(new)) then
        return new;                    -- the transfer cascade
    end if;
    select p.assessment_frozen_at is not null into frozen
    from position p where p.id = target_position;
    if frozen then
        raise exception 'position % has invited candidates — its assessment is frozen (FR-HIR-005)',
            target_position using errcode = 'raise_exception';
    end if;
    return coalesce(new, old);
end $$;

drop trigger if exists trg_stage_freeze on assessment_stage;
create trigger trg_stage_freeze before insert or update or delete on assessment_stage
    for each row execute function fn_stage_freeze();
