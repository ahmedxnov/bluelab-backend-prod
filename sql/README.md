# Versioned SQL modules

Not everything in the database comes from a migration. This directory holds the
objects data/04 §3 assigns to versioned SQL modules, re-applied idempotently at
every release and diffed by `tools/check_rls_drift.py`:

| Directory    | What                                                              | Source of truth |
|--------------|-------------------------------------------------------------------|-----------------|
| `roles/`     | Roles and grants — **not captured by `pg_dump`**, so this script is part of every restore and migration runbook | data/04 §3 |
| `functions/` | `fn_score_band` — the one banding function (FR-SCR-005)           | data/04 §3 |
| `views/`     | V-1…V-11, the derived-on-read aggregates (ADR-0034)               | data/02 §3 |
| `triggers/`  | The freeze guards, the decision freeze, the transcript guard      | data/00 §6 |
| `policies/`  | **Generated** — never hand-edited (ADR-0031)                      | `tools/generate_rls_policies.py` |

## Applying and checking

`tools/check_rls_drift.py` owns the order — `functions/`, then `views/` (the
numeric prefixes are a dependency order), then `triggers/`, then the regenerated
policies. It is the same order in both directions, because the check applies the
modules to diff them:

```
alembic upgrade head
python tools/check_rls_drift.py --apply    # bootstrap and release: apply, then verify
python tools/check_rls_drift.py            # commit gate row 4: read-only, rolls back
```

`roles/` is not in that order and is never applied by the drift checker. Roles are
cluster-level and `pg_dump` does not carry them. Environment provisioning runs the
parameterized `bootstrap.sql` before Alembic; Docker, the WSL fallback, and CI all
invoke that same executable source (data/04 §3, data/05 §3).

Two binding rules:

* **Customer-data views are always `security_invoker = true`.** A definer-semantics
  view owned by the migration role would silently bypass every RLS policy under
  it. The drift check asserts the reloption on the deployed view *and* on the
  repo's, so a view no file here defines is covered too (ADR-0031, data/02 §3).
* **Postgres-generic SQL only.** The extension allowlist is empty: no `citext`,
  no `pgcrypto`, no host-namespaced objects anywhere in the lineage. That is what
  keeps a host migration a connection-string change (data/04 §8).
