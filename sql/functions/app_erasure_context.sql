-- Transaction-bound freeze-guard capabilities for erasure and organization purge.
--
-- ADR-0033 authorizes the subject-erasure procedure; data/03 §6 authorizes
-- separately fenced organization-purge batches.
--
-- The guard checks a private capability keyed to the current backend and transaction.
-- Only the SECURITY DEFINER erasure procedure opens and closes that capability.
-- An application role can set a custom GUC, so no GUC grants this exemption.
-- Re-applied idempotently at every release, and diffed by
-- tools/check_rls_drift.py. NOT in the Alembic lineage (data/04 §3).

create or replace function app_in_erasure_context() returns boolean
language sql volatile security definer
set search_path = '' as
$$ select exists (
    select 1 from bluelab_internal.erasure_authorization
     where backend_pid = pg_catalog.pg_backend_pid()
       and xact_id = pg_catalog.txid_current()
) $$;

create or replace function app_in_purge_context() returns boolean
language sql volatile security definer
set search_path = '' as
$$ select exists (
    select 1 from bluelab_internal.purge_authorization
     where backend_pid = pg_catalog.pg_backend_pid()
       and xact_id = pg_catalog.txid_current()
) $$;

-- Did this UPDATE change anything beyond the denormalised scope columns?
--
-- ON UPDATE CASCADE rewrites org_id/team_id down whole subtrees on the two legal
-- ownership moves — a position transfer (FR-HIR-018) and a rep's team change
-- (FR-IDA-009). Those must pass a freeze guard: the content is untouched, only
-- its owner moved. Anything else is a real edit.
create or replace function app_scope_only_change(old_row jsonb, new_row jsonb)
returns boolean language sql immutable as
$$ select (old_row - 'org_id' - 'team_id') = (new_row - 'org_id' - 'team_id') $$;
