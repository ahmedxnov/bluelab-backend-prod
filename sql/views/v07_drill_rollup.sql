-- V-7 · per-drill rollup (FR-SCR-016, FR-TRM-009)
--
-- `security_invoker = true` IS NOT OPTIONAL. A definer-semantics view owned by
-- the migration role would run with that role's BYPASSRLS and silently return
-- every org's rows through a "read-only aggregate" (ADR-0031, data/02 §3). The
-- drift check asserts the reloption on every customer-data view.
--
-- Verbatim from data/02 §3 — that document is the definition, not a description.
-- Re-applied idempotently at every release, and diffed by
-- tools/check_rls_drift.py. NOT in the Alembic lineage (data/04 §3).

-- Graded attempts only.
create or replace view v_drill_stats with (security_invoker = true) as
select d.id as drill_id, d.org_id, d.team_id,
       count(distinct a.rep_account_id)            as reps_practiced,
       (select count(*) from account m
         where m.team_id = d.team_id and m.role = 'rep' and m.status = 'active') as eligible_reps,
       count(a.id)                                 as total_attempts,
       round(avg(s.overall_score), 1)              as average_score
from drill d
left join attempt a  on a.drill_id = d.id and a.status = 'graded' and a.rep_account_id is not null
left join scorecard s on s.attempt_id = a.id
where not d.self_authored
group by 1, 2, 3;
