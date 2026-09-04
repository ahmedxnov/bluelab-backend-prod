-- V-8 · per-participant drill statistics (FR-SCR-015)
--
-- `security_invoker = true` IS NOT OPTIONAL. A definer-semantics view owned by
-- the migration role would run with that role's BYPASSRLS and silently return
-- every org's rows through a "read-only aggregate" (ADR-0031, data/02 §3). The
-- drift check asserts the reloption on every customer-data view.
--
-- Verbatim from data/02 §3 — that document is the definition, not a description.
-- Re-applied idempotently at every release, and diffed by
-- tools/check_rls_drift.py. NOT in the Alembic lineage (data/04 §3).

-- Best, latest, average, and trend (latest vs first). INCLUDES self-authored
-- practice: it is excluded from ratings, not from the drill's own history
-- (FR-TRP-010).
create or replace view v_participant_drill_stats with (security_invoker = true) as
with graded as (
    select a.drill_id, a.rep_account_id, a.candidate_id, a.org_id, a.team_id, s.overall_score,
           first_value(s.overall_score) over w_desc as latest_score,
           first_value(s.overall_score) over w_asc  as first_score
    from attempt a join scorecard s on s.attempt_id = a.id
    where a.status = 'graded'
    window w_desc as (partition by a.drill_id, a.rep_account_id, a.candidate_id order by a.started_at desc),
           w_asc  as (partition by a.drill_id, a.rep_account_id, a.candidate_id order by a.started_at))
select drill_id, rep_account_id, candidate_id, org_id, team_id,
       max(overall_score)                        as best,
       max(latest_score)                         as latest,
       round(avg(overall_score), 1)              as average,
       round(max(latest_score) - max(first_score), 1) as trend,
       count(*)                                  as graded_attempts
from graded group by 1, 2, 3, 4, 5;
