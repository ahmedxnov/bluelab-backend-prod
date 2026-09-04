-- trg_decision_freeze — a decision freezes once it has gone to HR (FR-HIR-013).
--
-- Membership in a shortlist IS the freeze: the moment a candidate's report has
-- been sent, the decision that was sent cannot be quietly revised. A held-back
-- candidate simply has no membership row and stays approved-unsent.
--
-- Only the `decision` column is frozen. A manager may still edit their internal
-- note, and the transfer cascade may still rewrite scope.
--
-- Re-applied idempotently at every release; NOT in the Alembic lineage
-- (data/04 §3). Every guard has an L3 attack test that TRIES to breach it and
-- asserts refusal — the SQL equivalent of a surviving-mutant check
-- (quality/01 §4), because a trigger nobody attacks is a trigger nobody knows
-- works.
--
-- Erasure is the ONE sanctioned writer through these guards, recognised via
-- app_in_erasure_context() (ADR-0033).

create or replace function fn_decision_freeze() returns trigger
language plpgsql as $$
begin
    if app_in_erasure_context() then
        return new;
    end if;
    if new.decision is not distinct from old.decision then
        return new;                    -- not a decision change
    end if;
    if exists (select 1 from shortlist_candidate sc where sc.candidate_id = old.id) then
        raise exception
            'candidate % is on a sent shortlist — the decision is frozen (FR-HIR-013)',
            old.id using errcode = 'raise_exception';
    end if;
    return new;
end $$;

drop trigger if exists trg_decision_freeze on candidate;
create trigger trg_decision_freeze before update on candidate
    for each row execute function fn_decision_freeze();
