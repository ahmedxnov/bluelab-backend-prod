-- V-6 · gap analysis (FR-TRM-004)
--
-- `security_invoker = true` IS NOT OPTIONAL. A definer-semantics view owned by
-- the migration role would run with that role's BYPASSRLS and silently return
-- every org's rows through a "read-only aggregate" (ADR-0031, data/02 §3). The
-- drift check asserts the reloption on every customer-data view.
--
-- Verbatim from data/02 §3 — that document is the definition, not a description.
-- Re-applied idempotently at every release, and diffed by
-- tools/check_rls_drift.py. NOT in the Alembic lineage (data/04 §3).

-- Per call type, top performers' average against the bottom half's. Cohorts are
-- fixed by OVERALL monthly rating (the V-4 ranking), not per call type — so the
-- gap reads "what the strong reps do better here", not "who is worst at this".
-- Severity: >= 2.0 high, >= 1.0 moderate.
create or replace view v_gap_analysis with (security_invoker = true) as
with cohort as (
    select team_id, org_id, month, rep_account_id,
           tier = 'top'                        as is_top,
           rank_desc > ceil(rated_reps / 2.0)  as is_bottom_half
    from v_rep_month_tier where rated_reps >= 5)
select c.team_id, c.org_id, c.month, a.call_type,
       round(avg(a.overall_score) filter (where c.is_top), 1)          as top_avg,
       round(avg(a.overall_score) filter (where c.is_bottom_half), 1)  as bottom_half_avg,
       round(avg(a.overall_score) filter (where c.is_top)
           - avg(a.overall_score) filter (where c.is_bottom_half), 1)  as gap
from v_counted_attempt a
join cohort c on c.rep_account_id = a.rep_account_id
            and c.month = a.local_month
group by 1, 2, 3, 4;
