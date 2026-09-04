-- V-5 · team month (FR-TRM-002/004)
--
-- `security_invoker = true` IS NOT OPTIONAL. A definer-semantics view owned by
-- the migration role would run with that role's BYPASSRLS and silently return
-- every org's rows through a "read-only aggregate" (ADR-0031, data/02 §3). The
-- drift check asserts the reloption on every customer-data view.
--
-- Verbatim from data/02 §3 — that document is the definition, not a description.
-- Re-applied idempotently at every release, and diffed by
-- tools/check_rls_drift.py. NOT in the Alembic lineage (data/04 §3).

create or replace view v_team_month with (security_invoker = true) as
select team_id, org_id, month,
       round(avg(rating), 1)                                    as team_average,
       count(*) filter (where tier = 'top')                     as top_performers,
       count(*) filter (where tier = 'needs_coaching')          as needs_coaching,
       count(*)                                                 as rated_reps
from v_rep_month_tier
group by 1, 2, 3;
