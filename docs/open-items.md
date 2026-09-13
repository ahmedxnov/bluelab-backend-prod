# Open items

The register for everything deferred, undecided, or routed upstream. Same
convention the platform repository uses
([api/README](https://github.com/ahmedxnov/bluelab-platform/blob/5222f8b715dd9dc20b073c57c1f2be1eb7ce6feb/api/README.md),
[infra/README](https://github.com/ahmedxnov/bluelab-platform/blob/5222f8b715dd9dc20b073c57c1f2be1eb7ce6feb/infra/README.md),
[stack/00 §7](https://github.com/ahmedxnov/bluelab-platform/blob/5222f8b715dd9dc20b073c57c1f2be1eb7ce6feb/stack/00-selection-overview.stack.md)):
an item is closed when the artifact that owns it says so, not when it feels done.

**This file is the backlog. A commit message is not.** Anything discovered during
implementation lands here in the same diff that discovers it.

Status: `owner` needs a ruling · `deferred` has a named home · `verify` needs
checking before first real use.

---

## CI defects

### OI-21 · Backend GitHub Actions gate
**Status:** closed 2026-09-11 · **Raised:** Phase 1 Tasks 16–19 runner verification, 2026-09-06 · **Evidence:** [run 34564781218](https://github.com/ahmedxnov/bluelab-backend-prod/actions/runs/34564781218)

`backend-ci` completes on GitHub-hosted `ubuntu-24.04` for commit
`d6355e0ce88f8b75737f7b04bd7405e5c8c3658c`. The gate verifies the locked
contract and runtime snapshots, applies the Alembic lineage and generated SQL
modules to a clean PostgreSQL 16 database, and passes the static, architecture,
ratchet, and full test stages.

The green run emits a non-failing Node 20 deprecation warning for pinned action
revisions. Action-runtime maintenance remains a separate reviewed workflow change.

---

## Needs an owner ruling

### OI-1 · Signature amendment — `api/02 §1.2` and ADR-0071 decision 2
**Status:** closed 2026-09-05 · **Raised:** platform security pass · **Detail:** [call-plane-seam.md](call-plane-seam.md)

Both documents formerly specified the agent signature as *"HMAC-SHA256 over the exact raw
body"*. The `GET .../bundle` endpoint has an empty body, so that signature is a
**constant** — one observed header is a permanent credential for any `call_id`,
across tenants. Now implemented as `METHOD \n PATH \n TIMESTAMP \n sha256(body)`
with a 300s skew window.

The platform contract, backend verifier, and agent signer now use the same
method/path/timestamp/body-digest canonical string, enforce the 300-second skew
window, and share a fixed test vector.

### OI-2 · DST-nonexistent midnight in `clock.due_date_deadline`
**Status:** owner · **Raised:** platform code review

Egypt transitions at 00:00, and the deadline is "first instant of the next day,
org-locally". On a spring-forward date that local time does not exist; `ZoneInfo`
resolves it silently with the pre-transition offset, shifting the deadline by an
hour twice a year. Affects FR-TRM-002's *completed by due date*.

Two defensible answers — fold forward to the first real instant, or accept the
hour. It is a product policy question, not an implementation detail.

### OI-14 · `app.position_id` — amends ADR-0031 decision 1
**Status:** owner · **Raised:** RLS generator · **Detail:** [`platform/db/scope.py`](../src/bluelab/platform/db/scope.py)

ADR-0031 lists six GUCs and omits the position. But architecture/02 §3.2 defines
the candidate token binding as **`(org, position, candidate)`**, so the scope
tuple already contains it — the GUC list was under-specified against the trust
model rather than deliberately narrower.

Without it, the three candidate-journey policies each need a subquery back to
`candidate`, which is the classic RLS recursion hazard. With it they are plain
column comparisons. Also matches what `CandidateBinding` already returns.

### OI-15 · Four `SECURITY DEFINER` helpers, for confirmation
**Status:** owner · **Raised:** RLS generator

ADR-0031 sanctions enumerated definer escape hatches. The generator emits four:

    app_attempt_readable_by_account     attempt visibility, one hop
    app_scorecard_readable_by_account   scorecard children, two hops
    app_drill_in_team_positions         the FR-HIR-018 cross-team read
    app_candidate_in_team               email_send ownership (see OI-16)

Each is read-only, `STABLE`, takes only an id, returns a boolean, and has
`EXECUTE` revoked from `PUBLIC`. They exist because a policy predicate that reads
an RLS-protected table triggers that table's policies — a hidden per-row cost at
best, infinite recursion at worst. This is the enumerated list ADR-0031 asks for;
it wants confirming rather than assuming.

### OI-17 · Procrastinate's tables carry no RLS
**Status:** deferred → lands with the queue's own hardening · **Raised:** RLS security pass

Excluded from the completeness check because the queue is library-owned, so its
tables sit in the same database with no policies. Payloads are ids-only by
construction and `validate_payload` enforces it (api/02 §2), so the exposure is
org and entity ids rather than content — but a compromised request path reads
every org's queue.

Not fixed here because enabling RLS on tables a library reads and writes risks
breaking the library, and the fix wants the pinned Procrastinate version in front
of it (see OI-9).

*Two siblings of this item were closed rather than deferred: the `system` policy
no longer grants `for all` (the sweeps are `SECURITY DEFINER` procedures, not
policy-bound queries), and the privileged scope constructors moved to
`platform/db/privileged.py` behind an import-linter contract.*

### OI-16 · `email_send` has no `team_id`
**Status:** owner · **Raised:** RLS generator · **Would amend:** data/01 §8

Every other P8 table is team-scoped. `email_send` carries `org_id` only, so the
owning manager's read of E-2 delivery state (FR-HIR-010) has to hop through
`candidate` via a definer helper.

A denormalised `team_id` would make it a plain predicate like its siblings, and
the scope-column standard (data/00 §3) says team-scoped tables carry one. This
looks like an oversight in data/01 §8 rather than a decision — but it is data's
call, not the implementation's.

### OI-3 · `Settings` uses `extra="forbid"` with an `env_file`
**Status:** owner · **Raised:** platform code review

pydantic-settings reads *every* key in `.env`, so an unrelated variable someone
adds for a shell script stops the app booting. Strict is right for catching
env-var typos and wrong for a shared dotenv. Both are defensible; pick one.

### OI-4 · The Tier-1 object store is unowned
**Status:** owner · **Raised:** stack coverage check

C-11's *launch* implementation was **Supabase Storage** ([stack/00 §9](https://github.com/ahmedxnov/bluelab-platform/blob/5222f8b715dd9dc20b073c57c1f2be1eb7ce6feb/stack/00-selection-overview.stack.md)).
Amendment B (owner, 2026-07-24) put the rollout on RDS at T1, so Supabase is
never provisioned — and nothing then answers what serves the object store at T1.
The T1 compose list in [infra/01 §7](https://github.com/ahmedxnov/bluelab-platform/blob/5222f8b715dd9dc20b073c57c1f2be1eb7ce6feb/infra/01-topology-and-networking.infra.md)
is `caddy · app · work · call-agent · valkey · backup(cron)`: no MinIO, and no S3
bucket named.

**Bites at deploy, not at code.** ADR-0025 makes the S3 API the contract, so the
adapter is unaffected either way — but the box has to store recordings and report
PDFs somewhere before the first real call.

### OI-5 · `infra/00 §1` and `stack/00 §9` still describe the Supabase launch path
**Status:** owner · **Raised:** stack coverage check

Amendment B superseded it — LADDER records the Supabase→AWS migration as
*"discharged at T1"* — but neither document was updated. `infra/00 §1` still
lists staging and prod Postgres as Supabase projects, and `stack/00 §9` is an
entire section planning a migration that will not happen. Risk **R-17** likewise
describes a cross-provider seam that never exists.

No code impact — the schema is host-neutral by mandate (data/04 §8) — but the
documents will mislead the next reader.

---

## Deferred, with a named home

### OI-18 · Current legal gates and account scope
**Status:** resolved by the authorized identity gate patch, 2026-09-11.

Request resolution now reloads active account state, team membership and legal
gates from PostgreSQL before product access. This closes the existing-session
bypass even when a legal version is inserted directly by a migration. The earlier
publish-time fan-out proposal remains an optimization only; a future replacement
must preserve next-request enforcement, including scheduled effective versions.

Missing effective documents cannot clear onboarding or record partial evidence.
Recording consent uses `recording_consent_notice`; terms acceptance requires the
exact `terms_of_use`/`privacy_notice` pair. Future versions are ignored until
effective, and equal effective timestamps have an immutable-id tie-break.

Deactivated/missing accounts and changed roles invalidate the current cookie.
Team changes are reflected before product queries. A stored limited cookie whose
gate was cleared on another device requires reauthentication rather than silently
gaining privileges. The accepting device retains the existing rotation path.

Regression evidence: `tests/l3_integration/auth/test_current_identity_gates.py`.
Apply the regenerated helpers through `tools/check_rls_drift.py --apply` during
the normal database bootstrap/release step before running the patched application.

### OI-6 · `revoke_all` has a race
**Status:** closed 2026-09-11 · **Raised:** SEC-F7

Customer and operations sessions carry a per-account revocation generation.
`revoke_all()` increments it before reading the session index; creation rejects a
generation captured before revocation, and resolution checks it before and after
sliding the idle window. Per-session tombstones prevent a stale concurrent
resolver from restoring a signed-out or rotated cookie. Sign-in snapshots the
generation before its current-account re-check, so a concurrent credential or
scope revocation cannot issue a session against the newer generation.

Regression evidence: `tests/l1_unit/test_session_revocation_race.py` and the
session revocation journeys under `tests/l3_integration/auth/` and
`tests/l3_integration/operations/`.

### OI-7 · Password policy
**Status:** closed 2026-09-13 · **Raised:** SEC-F8

Every password-setting request enforces the contract's 12–1024 character range
before Argon2id hashing. The shared password service applies the same bounds to
first-sign-in and reset completion, and the API schemas expose the matching
validation contract.

Regression evidence: `tests/l1_unit/test_passwords.py` and
`tests/l3_integration/auth/test_auth_gates.py`.

### OI-8 · No timeout on Valkey calls
**Status:** closed 2026-09-11 · **Raised:** platform security pass

The coordination client applies the shared `DEPENDENCY_TIMEOUT_SECONDS` bound to
both connection establishment and socket operations. The validated default is
five seconds and deployment configuration may select a value from greater than
zero through thirty seconds. Regression evidence:
`tests/l1_unit/test_rate_limit_health.py`.

### OI-19 · `GET /me/library` re-counts the whole grid on every page
**Status:** deferred → needs L6 · **Raised:** `/me/library` security review

`LibraryPage.counts` is required on **every** page (FR-TRP-007, live per-tab
counts), so the count cannot be computed once on page one and carried. It is a
second statement over the same CTE chain with no `LIMIT`, which means paging a
large library re-aggregates the caller's entire visible set per request, and the
chain runs twice per request either way.

Bounded today by the surface rate limit (600/60s) and by per-rep libraries being
small. It is nonetheless the part of this surface that degrades first, and the
degradation is invisible until a team has thousands of drills.

Two directions if it ever bites, neither free: return counts only on the first
page (a contract change — `counts` is `required`), or maintain them outside the
read path (an aggregate table, which ADR-0034 rules out for exactly the reason
FR-TRP-002 needs — one definition, derived on read).

**Not measurable here.** See OI-20.

### OI-20 · No read path on this surface has been measured at scale
**Status:** deferred → L6 does not exist · **Raised:** `/me/library` code review

The L3 fixtures hold six drills. Every plan over them is a toy: index choice is
arbitrary at that size, timings are noise, and a sequential scan is genuinely the
right answer. So the performance claims for `/me/library` — and for `/me/progress`
before it — are **unverified, not verified-good**.

What *is* established for `/me/library`: the universe is a `UNION ALL` rather than
an `OR`, so each branch can reach its own index (`idx_drill_team_status` /
`idx_drill_author_self`, both named in
[data/02 §2](https://github.com/ahmedxnov/bluelab-platform/blob/5222f8b715dd9dc20b073c57c1f2be1eb7ce6feb/data/02-query-patterns-and-indexes.data.md)). Confirmed
reachable by forcing `enable_seqscan = off`. Reachable is not chosen — at six
rows the planner has no reason to prefer either, and that is the whole point of
this item.

An earlier attempt added `team_id` to a single `OR` predicate and did **not**
help: PostgreSQL satisfies one index condition per scan, so an `OR` across two
differently-shaped branches collapses to the widest common predicate (`org_id`)
with the rest as a filter. Recorded because it is the obvious fix and it is wrong.

Needs the L6 load suite (zero files today) with a realistically-sized org.

---

## Verify before first real use

### OI-9 · The Procrastinate table shape is unpinned
**Status:** closed 2026-09-11 · **Source:** banked DoD item, [quality/08 §4](https://github.com/ahmedxnov/bluelab-platform/blob/5222f8b715dd9dc20b073c57c1f2be1eb7ce6feb/quality/08-definition-of-done-and-the-build-loop.quality.md), data **F-9**

The hash-locked runtime pins Procrastinate 3.9.0. Transactional enqueue targets
that version's installed table shape and commits with the domain write in the
same PostgreSQL transaction. L3 rollback/commit tests exercise the property, and
the containerized worker reaches a healthy database-backed heartbeat.

### OI-10 · Two library assumptions
**Status:** closed 2026-09-11

- The hash-locked runtime pins Valkey 6.1.1; session, throttle, replay, and
  pipeline behavior runs against both fakeredis and a real Valkey 8 process.
- The hash-locked runtime pins OpenTelemetry API/SDK 1.44.0 and instrumentation
  0.65b0; telemetry imports and the content-free telemetry gate execute in CI.

### OI-11 · Nothing in `platform/` has been executed
**Status:** closed 2026-09-11

The backend suite imports and executes the platform wiring. Ruff, mypy,
import-linter, telemetry, cookie, tier-branch, fixture-safety, contract-lock,
conformance, and requirement gates cover its static boundaries.

---

## Cross-repo

### OI-12 · The agent does not match the contract
**Status:** closed 2026-09-05 · **Detail:** [call-plane-seam.md](call-plane-seam.md)

`bluelab-agent-prod` calls the retired spec set's endpoints, keys the seam on
`attempt_id` (which cannot serve a test call — FR-DRL-012 creates no attempt),
has no interruption endpoint at all, and streams transcript segments mid-call.
The agent now uses the three call-keyed endpoints, buffers the transcript until completion, reports
interruption separately, and mirrors the canonical method/path/timestamp/body signature.

### OI-13 · The PDF token source crosses a repository boundary
**Status:** closed 2026-09-05 · **Detail:** [../templates/README.md](../templates/README.md)

ADR-0042 requires one token source for both the SPA and the report PDF, but that
source is `theme.css` in `bluelab-frontend`. How it reaches the template is
exported as a versioned CSS artifact and vendored here with source commit and
SHA-256 lock. CI verifies the local bytes; no sibling checkout is required.

---

## Accepted deviations

Reviewed, deliberately kept, and written down so the next reviewer reaches the
same conclusion without re-deriving it. Not backlog — nothing here is waiting on
anything.

### AD-1 · `select *` inside the `/me/library` CTE chain
Flagged by review as a production-query smell. Kept: `filtered` and `ordered`
project from the CTE directly above them, so the column set is already fixed and
there is no over-fetch from a table. Naming ~14 columns at all three levels would
triple the noise around the bucket arithmetic, which is the part worth reading.
The rule is aimed at `select *` against a *table*, and that does not occur here.

### AD-2 · `training.service.library()` takes nine parameters
Over the five-parameter threshold. Each one is a query parameter the contract
declares (`source`, `call_type`, `attempted`, `sort`, `cursor`, `limit`) plus the
two scope values and the session. Bundling them into a filter object would add a
type that exists only to satisfy a count, and the signature would still have to
be unpacked at exactly one call site.

### AD-3 · A library cursor is not bound to the account that minted it
Raised by the security review as a possible IDOR. It is not: the `WHERE` is
account-scoped independently of the cursor, so replaying someone else's position
can only skip *your own* rows — something you could do by crafting your own.
Binding the account would let replay be *detected*, but there is nothing to
prevent. Worth knowing if a cursor ever starts carrying a filter rather than a
position, at which point this stops being true.
