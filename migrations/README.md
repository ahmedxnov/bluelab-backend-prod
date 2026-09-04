# Migrations

Alembic, **one linear lineage, one head** (data/04 §1). Autogenerate is a draft
aid only — every migration is reviewed DDL, and RLS policies, triggers, views, and
functions never come from autogenerate; they live in `../sql/` and are re-applied
idempotently at every release.

**Forward-only.** Down-migrations are not written and not trusted: the rollback of
record for a bad migration is point-in-time restore plus a forward fix. At this
team size honest PITR beats fictional `downgrade()` code.

## The compatibility window — N and N+1 coexist

Application and work planes roll instance-by-instance, and the call plane drains
for up to **15 minutes** — a draining worker still executes completion writes with
release-N code. Therefore:

> Every migration must be compatible with the released application version N while
> version N+1 rolls out. Breaking changes are staged across releases
> (expand -> migrate -> contract), never done in one.

A diff under this directory triggers the **window test on the pull request**, not
only on release: version-N integration tests against the N+1-migrated clone,
job-payload compatibility in both directions, and the unread-evidence check on any
contract migration (pipeline/02 §3, gate F-7).

## Seeds

Platform reference rows are **data migrations** — idempotent upserts with fixed
literal UUIDs, environment-invariant. The v1 vertical seed is *not* a seed
migration: it is customer data, created through ops provisioning and the product's
own flows (data/04 §6, `../seeds/`).

## Portability

Postgres-generic SQL only; the extension allowlist is empty. Hashing and UUIDv7
generation are application-side — the database never mints identity (data/04 §8).
