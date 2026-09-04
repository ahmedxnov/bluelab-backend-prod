-- trg_concealed_freeze — the concealed set freezes with its drill (FR-DRL-015).
--
-- Challenges and hidden motives are what the participant is meant to discover.
-- Once a drill is published and people have taken it, editing them retroactively
-- changes what past attempts were graded against.
--
-- Re-applied idempotently at every release; NOT in the Alembic lineage
-- (data/04 §3). Every guard has an L3 attack test that TRIES to breach it and
-- asserts refusal — the SQL equivalent of a surviving-mutant check
-- (quality/01 §4), because a trigger nobody attacks is a trigger nobody knows
-- works.
--
-- Erasure is the ONE sanctioned writer through these guards, recognised via
-- app_in_erasure_context() (ADR-0033).

create or replace function fn_concealed_freeze() returns trigger
language plpgsql as $$
declare
    target_drill uuid := coalesce(new.drill_id, old.drill_id);
    published boolean;
begin
    if app_in_erasure_context() then
        return coalesce(new, old);
    end if;
    if tg_op = 'UPDATE' and app_scope_only_change(to_jsonb(old), to_jsonb(new)) then
        return new;                    -- an ownership move, not an edit
    end if;
    select d.status in ('published', 'archived') into published
    from drill d where d.id = target_drill;
    if published then
        raise exception 'drill % is published — its concealed set is frozen (FR-DRL-015)',
            target_drill using errcode = 'raise_exception';
    end if;
    return coalesce(new, old);
end $$;

drop trigger if exists trg_concealed_freeze on drill_concealed;
create trigger trg_concealed_freeze before update or delete on drill_concealed
    for each row execute function fn_concealed_freeze();
