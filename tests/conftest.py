"""Shared fixtures.

Bring-up is `docker compose up` (postgres:16, valkey, minio, mailpit) — local
parity with staging and prod on engine version and on the migration lineage
(infra/00 §2). Vendor calls run in **recorded-fixture mode** by default:
deterministic tests without spend, and the fixture set is versioned with the
repository (infra/00 §3).
"""
