# Local bootstrap

Run the complete backend-owned local setup from this repository root:

```powershell
py -3.12 scripts/bootstrap_local.py
```

The command creates or refreshes `.venv` from `requirements-dev.lock`, starts the
repository's Compose services, applies the complete Alembic lineage (including
the fixed platform-reference seed), and applies the versioned SQL modules via the
RLS drift tool. It does not create customer data.

## Timing evidence

Timing results are recorded after each clean local-bootstrap run in this file.

| Date | Host / method | Duration | Result |
|---|---|---:|---|
| 2026-09-05 | Windows 11; fresh virtual environment and isolated fresh Compose volumes | ~32 s | Passed; PostgreSQL 16, Alembic through `0003_platform_reference_seed`, and generated SQL modules applied. |
