-- V-1 · the counted pool (FR-TRM-002)
--
-- `security_invoker = true` IS NOT OPTIONAL. A definer-semantics view owned by
-- the migration role would run with that role's BYPASSRLS and silently return
-- every org's rows through a "read-only aggregate" (ADR-0031, data/02 §3). The
-- drift check asserts the reloption on every customer-data view.
--
-- Verbatim from data/02 §3 — that document is the definition, not a description.
-- Re-applied idempotently at every release, and diffed by
-- tools/check_rls_drift.py. NOT in the Alembic lineage (data/04 §3).

-- Graded rep attempts on team drills. Self-authored practice is excluded
-- (FR-TRP-010); test calls have no rows at all; interrupted attempts are never
-- graded (AC-SCR-007). `local_month` resolves "calendar month" to the org's
-- civil calendar — the month the rep lived, not the UTC month.
create or replace view v_counted_attempt with (security_invoker = true) as
select a.id as attempt_id, a.org_id, a.team_id, a.rep_account_id, a.drill_id,
       d.call_type, a.started_at,
       date_trunc('month', a.started_at at time zone o.timezone) as local_month,
       s.overall_score
from attempt a
join scorecard s on s.attempt_id = a.id
join drill d     on d.id = a.drill_id
join org o       on o.id = a.org_id
where a.status = 'graded' and a.rep_account_id is not null and not a.self_authored;
