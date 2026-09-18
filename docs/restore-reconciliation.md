# Isolated restore reconciliation

The recovery sequence follows [data/05 §4](https://github.com/ahmedxnov/bluelab-platform/blob/5222f8b715dd9dc20b073c57c1f2be1eb7ce6feb/data/05-backup-and-disaster-recovery.data.md). The restored database and object copy remain isolated from customer traffic, direct object reads, previously issued bearer URLs, integrations, and ordinary workers. An operator verifies that isolation at the storage and routing layers before setting `RESTORE_ISOLATION_CONFIRMED=true`.

Bootstrap roles, restore the database, apply Alembic migrations, and run SQL drift and isolation checks. Set `DATABASE_URL` and `MIGRATION_DATABASE_URL` to the same isolated restore database using its migration role. Configure both lifecycle-history authorities, both erasure-ledger copies, the restored object store, and Valkey. Then run:

```powershell
$env:RESTORE_ISOLATION_CONFIRMED = 'true'
.\.venv\Scripts\python.exe -m bluelab.entrypoints.restore_reconcile
```

The command verifies the complete organization catalog, dual-copy receipts and event chains, reconstructs rollback-lost lifecycle decisions, resumes authorized purge work, replays armed erasure markers, and reconciles objects by verified ownership. It exits unsuccessfully if an identifier, event, manifest, ownership link, restriction, deletion, or independent source cannot be verified. A successful exit supplies reconciliation evidence for the operator's remaining recovery checks and release decision; it does not open traffic or start ordinary workers.

The release record contains the successful command exit, current database isolation and SQL drift results, and proof that restored objects and previously issued bearer URLs remain gated. Run edge sign-in, one admission, and one review render in the isolated recovery environment. Release affected traffic and resume ordinary workers only after all three smokes and reconciliation succeed. A failed command or smoke keeps both held while the operator resolves the finding and reruns reconciliation.

The frontend repository's `npm run test:restore` runs those three browser checks against a synthetic operations account, a synthetic customer account, an assigned published drill, and a graded review. Supply `BLUELAB_E2E_OPS_EMAIL`, `BLUELAB_E2E_OPS_PASSWORD`, `BLUELAB_E2E_OPS_TOTP_SEED`, `BLUELAB_E2E_ACCOUNT_EMAIL`, `BLUELAB_E2E_ACCOUNT_PASSWORD`, `BLUELAB_E2E_DRILL_ID`, and `BLUELAB_E2E_REVIEW_ATTEMPT_ID` through the test environment. Set `BLUELAB_RESTORE_BASE_URL` to the isolated deployment's HTTPS edge URL; the test then checks the edge scheme and operations cookie attributes. With that URL unset, the test uses the local Vite proxy as an application-path rehearsal. Record the test result and verify the ordinary worker's healthy state only after it passes.
