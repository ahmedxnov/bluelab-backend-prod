-- V-2 · rep monthly rating (FR-TRM-002, FR-TRP-001)
--
-- `security_invoker = true` IS NOT OPTIONAL. A definer-semantics view owned by
-- the migration role would run with that role's BYPASSRLS and silently return
-- every org's rows through a "read-only aggregate" (ADR-0031, data/02 §3). The
-- drift check asserts the reloption on every customer-data view.
--
-- Verbatim from data/02 §3 — that document is the definition, not a description.
-- Re-applied idempotently at every release, and diffed by
-- tools/check_rls_drift.py. NOT in the Alembic lineage (data/04 §3).

create or replace view v_rep_monthly_rating with (security_invoker = true) as
select org_id, team_id, rep_account_id, local_month as month,
       round(avg(overall_score), 1)                                              as rating,
       round(avg(overall_score) filter (where call_type = 'discovery'), 1)       as rating_discovery,
       round(avg(overall_score) filter (where call_type = 'post_proposal'), 1)   as rating_post_proposal,
       round(avg(overall_score) filter (where call_type = 'renewal'), 1)         as rating_renewal,
       round(avg(overall_score) filter (where call_type = 'upsell'), 1)          as rating_upsell,
       count(*)                                                                  as counted_attempts
from v_counted_attempt
group by 1, 2, 3, 4;
