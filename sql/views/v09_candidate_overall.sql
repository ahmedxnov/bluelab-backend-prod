-- V-9 · candidate overall (FR-HIR-011)
--
-- `security_invoker = true` IS NOT OPTIONAL. A definer-semantics view owned by
-- the migration role would run with that role's BYPASSRLS and silently return
-- every org's rows through a "read-only aggregate" (ADR-0031, data/02 §3). The
-- drift check asserts the reloption on every customer-data view.
--
-- Verbatim from data/02 §3 — that document is the definition, not a description.
-- Re-applied idempotently at every release, and diffed by
-- tools/check_rls_drift.py. NOT in the Alembic lineage (data/04 §3).

-- The UNWEIGHTED mean of per-drill overalls, plus total time (AC-HIR-003).
-- Unweighted is the requirement: hiring uses per-drill rubrics only, and
-- weighting across drills would silently rank one stage above another.
create or replace view v_candidate_overall with (security_invoker = true) as
select c.id as candidate_id, c.org_id, c.team_id, c.position_id,
       round(avg(s.overall_score), 1) as overall_score,
       count(*)                       as drills_graded,
       sum(a.duration_seconds)        as total_seconds,
       bool_or(a.restart)             as any_restart
from candidate c
join attempt a   on a.candidate_id = c.id and a.status = 'graded'
join scorecard s on s.attempt_id = a.id
group by 1, 2, 3, 4;
