-- V-4 · performance tiers (FR-TRM-003)
--
-- `security_invoker = true` IS NOT OPTIONAL. A definer-semantics view owned by
-- the migration role would run with that role's BYPASSRLS and silently return
-- every org's rows through a "read-only aggregate" (ADR-0031, data/02 §3). The
-- drift check asserts the reloption on every customer-data view.
--
-- Verbatim from data/02 §3 — that document is the definition, not a description.
-- Re-applied idempotently at every release, and diffed by
-- tools/check_rls_drift.py. NOT in the Alembic lineage (data/04 §3).

-- Top/bottom 20% rounded to the nearest whole rep, minimum one, SUPPRESSED under
-- five rated reps — a "bottom performer" out of three is a naming exercise, not a
-- finding. Deterministic tie-break by id so two renders never disagree.
create or replace view v_rep_month_tier with (security_invoker = true) as
with ranked as (
    select r.*,
           count(*)     over (partition by team_id, month)                                    as rated_reps,
           row_number() over (partition by team_id, month order by rating desc, rep_account_id) as rank_desc
    from v_rep_monthly_rating r)
select *,
       case when rated_reps < 5 then null
            when rank_desc <= greatest(1, round(rated_reps * 0.20)) then 'top'
            when rank_desc >  rated_reps - greatest(1, round(rated_reps * 0.20)) then 'needs_coaching'
            else 'mid' end as tier
from ranked;
