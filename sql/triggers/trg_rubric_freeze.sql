-- trg_rubric_freeze — the rubric freezes with its drill (FR-DRL-015).
--
-- INSERT is guarded too, not just UPDATE and DELETE: adding a dimension to a
-- published drill would change what every future attempt is scored against while
-- leaving past scorecards computed on the old set. Regeneration replaces the
-- rubric wholesale, and only while the drill is a draft.
--
-- Re-applied idempotently at every release; NOT in the Alembic lineage
-- (data/04 §3). Every guard has an L3 attack test that TRIES to breach it and
-- asserts refusal — the SQL equivalent of a surviving-mutant check
-- (quality/01 §4), because a trigger nobody attacks is a trigger nobody knows
-- works.
--
-- Erasure is the ONE sanctioned writer through these guards, recognised via
-- app_in_erasure_context() (ADR-0033).

create or replace function fn_rubric_freeze() returns trigger
language plpgsql as $$
declare
    target_drill uuid := coalesce(new.drill_id, old.drill_id);
    published boolean;
begin
    if app_in_erasure_context() then
        return coalesce(new, old);
    end if;
    if tg_op = 'UPDATE' and app_scope_only_change(to_jsonb(old), to_jsonb(new)) then
        return new;
    end if;
    select d.status in ('published', 'archived') into published
    from drill d where d.id = target_drill;
    if published then
        raise exception 'drill % is published — its rubric is frozen (FR-DRL-015)',
            target_drill using errcode = 'raise_exception';
    end if;
    return coalesce(new, old);
end $$;

drop trigger if exists trg_rubric_freeze on rubric_dimension;
create trigger trg_rubric_freeze before insert or update or delete on rubric_dimension
    for each row execute function fn_rubric_freeze();
