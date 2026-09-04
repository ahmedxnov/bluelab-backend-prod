-- V-11 · position pipeline counts (FR-HIR-001/002)
--
-- `security_invoker = true` IS NOT OPTIONAL. A definer-semantics view owned by
-- the migration role would run with that role's BYPASSRLS and silently return
-- every org's rows through a "read-only aggregate" (ADR-0031, data/02 §3). The
-- drift check asserts the reloption on every customer-data view.
--
-- Verbatim from data/02 §3 — that document is the definition, not a description.
-- Re-applied idempotently at every release, and diffed by
-- tools/check_rls_drift.py. NOT in the Alembic lineage (data/04 §3).

-- invited · pending review · approved-unsent.
create or replace view v_position_counts with (security_invoker = true) as
select p.id as position_id, p.org_id, p.team_id,
       count(c.id)                                                          as invited,
       count(c.id) filter (where c.completed_at is not null
                             and c.decision = 'pending')                    as pending_review,
       count(c.id) filter (where c.decision = 'approved' and not exists
             (select 1 from shortlist_candidate sc where sc.candidate_id = c.id)) as approved_unsent
from position p left join candidate c on c.position_id = p.id
group by 1, 2, 3;
