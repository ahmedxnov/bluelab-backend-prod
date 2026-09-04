-- The erasure context — the one sanctioned way through a freeze guard.
--
-- ADR-0033: erasure is the single writer permitted to cross the freeze guards,
-- through a dedicated procedure the guards recognise. This is that recognition.
--
-- The procedure sets `app.erasure_context` for the duration of its transaction
-- (SET LOCAL, so it cannot leak to the next one on a pooled connection), and the
-- guards call this to decide whether to yield.
--
-- It is deliberately NOT settable from a request path: nothing in
-- platform.db.scope writes this GUC, and the erasure procedure is reachable only
-- from the ops surface (ADR-0031's enumerated escape hatches).
-- Re-applied idempotently at every release, and diffed by
-- tools/check_rls_drift.py. NOT in the Alembic lineage (data/04 §3).

create or replace function app_in_erasure_context() returns boolean
language sql stable as
$$ select coalesce(nullif(current_setting('app.erasure_context', true), ''), 'off') = 'on' $$;

-- Did this UPDATE change anything beyond the denormalised scope columns?
--
-- ON UPDATE CASCADE rewrites org_id/team_id down whole subtrees on the two legal
-- ownership moves — a position transfer (FR-HIR-018) and a rep's team change
-- (FR-IDA-009). Those must pass a freeze guard: the content is untouched, only
-- its owner moved. Anything else is a real edit.
create or replace function app_scope_only_change(old_row jsonb, new_row jsonb)
returns boolean language sql immutable as
$$ select (old_row - 'org_id' - 'team_id') = (new_row - 'org_id' - 'team_id') $$;
