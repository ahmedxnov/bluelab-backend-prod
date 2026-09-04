-- fn_score_band — the ONE banding function (FR-SCR-005).
--
-- green >= 7.5 · amber >= 5.5 · red below. Every rendered score is accompanied
-- by its band, computed here; clients render and never re-derive (api/00 §2,
-- ux/00 §6 invariant 5). IMMUTABLE, so it is indexable and inlinable.
--
-- Verbatim from data/02 §3.
-- Re-applied idempotently at every release, and diffed by
-- tools/check_rls_drift.py. NOT in the Alembic lineage (data/04 §3).

create or replace function fn_score_band(score numeric) returns text
language sql immutable as
$$ select case when score >= 7.5 then 'green' when score >= 5.5 then 'amber' else 'red' end $$;
