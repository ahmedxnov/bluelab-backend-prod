-- V-3 · trend vs the CALENDAR-prior month (FR-TRP-001, FR-TRM-006)
--
-- `security_invoker = true` IS NOT OPTIONAL. A definer-semantics view owned by
-- the migration role would run with that role's BYPASSRLS and silently return
-- every org's rows through a "read-only aggregate" (ADR-0031, data/02 §3). The
-- drift check asserts the reloption on every customer-data view.
--
-- Verbatim from data/02 §3 — that document is the definition, not a description.
-- Re-applied idempotently at every release, and diffed by
-- tools/check_rls_drift.py. NOT in the Alembic lineage (data/04 §3).

-- Calendar-prior, not "30 days ago" — a distinction that matters in February.
create or replace view v_rep_month_trend with (security_invoker = true) as
select cur.*, round(cur.rating - prev.rating, 1) as trend
from v_rep_monthly_rating cur
left join v_rep_monthly_rating prev
  on prev.rep_account_id = cur.rep_account_id and prev.month = cur.month - interval '1 month';
