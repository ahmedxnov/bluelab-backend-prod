-- trg_drill_freeze — published drill content is immutable (FR-DRL-015).
--
-- Comparability is why: two candidates on one position must face the identical
-- drill, and a rep's month-old score must mean what it meant then. Changing
-- content means a NEW drill, never an edit.
--
-- The only permitted post-publish change is archival: status -> 'archived' with
-- its timestamp. Everything else — scenario, answer_key, rubric, label,
-- content_hash — is frozen.
--
-- Implemented by diffing the whole row as jsonb minus the allowed keys, rather
-- than by listing the frozen columns. A column added later is frozen by default;
-- an allow-list would silently exempt it.
--
-- Re-applied idempotently at every release; NOT in the Alembic lineage
-- (data/04 §3). Every guard has an L3 attack test that TRIES to breach it and
-- asserts refusal — the SQL equivalent of a surviving-mutant check
-- (quality/01 §4), because a trigger nobody attacks is a trigger nobody knows
-- works.
--
-- Erasure is the ONE sanctioned writer through these guards, recognised via
-- app_in_erasure_context() (ADR-0033).

create or replace function fn_drill_freeze() returns trigger
language plpgsql as $$
begin
    if app_in_erasure_context() then
        return new;
    end if;
    if old.status not in ('published', 'archived') then
        return new;                    -- drafts are freely editable
    end if;
    if (to_jsonb(old) - 'status' - 'archived_at' - 'updated_at')
       <> (to_jsonb(new) - 'status' - 'archived_at' - 'updated_at') then
        raise exception 'drill % is published — content is frozen (FR-DRL-015)', old.id
            using errcode = 'raise_exception';
    end if;
    if new.status <> old.status and not (old.status = 'published' and new.status = 'archived') then
        raise exception 'drill %: only published -> archived is permitted, not % -> %',
            old.id, old.status, new.status using errcode = 'raise_exception';
    end if;
    return new;
end $$;

drop trigger if exists trg_drill_freeze on drill;
create trigger trg_drill_freeze before update on drill
    for each row execute function fn_drill_freeze();
