-- V-10 · candidate journey state (FR-HIR-010/016)
--
-- `security_invoker = true` IS NOT OPTIONAL. A definer-semantics view owned by
-- the migration role would run with that role's BYPASSRLS and silently return
-- every org's rows through a "read-only aggregate" (ADR-0031, data/02 §3). The
-- drift check asserts the reloption on every customer-data view.
--
-- Verbatim from data/02 §3 — that document is the definition, not a description.
-- Re-applied idempotently at every release, and diffed by
-- tools/check_rls_drift.py. NOT in the Alembic lineage (data/04 §3).

-- Derived, never stored. `expired_incomplete` is what makes the pipeline honest
-- about a candidate whose link lapsed before they finished.
create or replace view v_candidate_state with (security_invoker = true) as
select c.*,
       case when c.completed_at is not null then 'completed'
            when exists (select 1 from attempt a where a.candidate_id = c.id) then 'in_progress'
            else 'invited' end as journey_state,
       (c.completed_at is null and not exists
            (select 1 from candidate_token t
              where t.candidate_id = c.id and t.revoked_at is null and t.expires_at > now())
       ) as expired_incomplete
from candidate c;
