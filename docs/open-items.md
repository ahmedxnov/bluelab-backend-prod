# Open items

The register for everything deferred, undecided, or routed upstream. Same
convention the phase folders use ([api/README](../../../api/README.md),
[infra/README](../../../infra/README.md), [stack/00 §7](../../../stack/00-selection-overview.stack.md)):
an item is closed when the artifact that owns it says so, not when it feels done.

**This file is the backlog. A commit message is not.** Anything discovered during
implementation lands here in the same diff that discovers it.

Status: `owner` needs a ruling · `deferred` has a named home · `verify` needs
checking before first real use.

---

## Needs an owner ruling

### OI-1 · Signature amendment — `api/02 §1.2` and ADR-0071 decision 2
**Status:** owner · **Raised:** platform security pass · **Detail:** [call-plane-seam.md](call-plane-seam.md)

Both documents specify the agent signature as *"HMAC-SHA256 over the exact raw
body"*. The `GET .../bundle` endpoint has an empty body, so that signature is a
**constant** — one observed header is a permanent credential for any `call_id`,
across tenants. Now implemented as `METHOD \n PATH \n TIMESTAMP \n sha256(body)`
with a 300s skew window.

**The code is ahead of the contract.** Needs sign-off, then the upstream edit.
The agent must mirror it — and the agent is changing anyway (OI-12), so it lands
in the same pass.

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

C-11's *launch* implementation was **Supabase Storage** ([stack/00 §9](../../../stack/00-selection-overview.stack.md)).
Amendment B (owner, 2026-07-24) put the rollout on RDS at T1, so Supabase is
never provisioned — and nothing then answers what serves the object store at T1.
The T1 compose list in [infra/01 §7](../../../infra/01-topology-and-networking.infra.md)
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

### OI-18 · A gate opened mid-session is not enforced until the next sign-in
**Status:** owner-ruled, no trigger exists yet → lands with the legal-document
write path · **Raised:** the auth review pass

`pending_gates` is derived on read, but what *enforces* a gate is
`SessionRecord.gate`, and that is written at sign-in. So publishing a new privacy
notice or terms version limits an account **from its next sign-in**, not from its
next request. `GET /auth/session` reports the gate honestly; nothing stops a
client that ignores the answer.

**Owner ruling: re-gate live sessions at publish time**, rather than re-deriving
on every authenticated request. Publishing a legal document happens a few times a
year and authenticated requests happen constantly, so the work belongs on the rare
event. The rejected alternative costs 2–3 extra queries on every call, forever, to
close a window that opens almost never.

**Not implemented, because there is nothing to hook.** `legal_document_version` is
`P0_REFERENCE` and the contract exposes exactly one Legal operation —
`GET /api/v1/legal-documents`. Publishing today is a migration or an ops
statement, so there is no application code path where a re-gate could run. The
mechanism lands *with* the write path when one is built; `SessionStore` already
has the account index that a fan-out would walk.

Until then the enforcement boundary is sign-in, and `gates.py`'s module docstring
says so rather than implying the stronger guarantee it used to claim.

### OI-6 · `revoke_all` has a race
**Status:** deferred → identity module · **Raised:** SEC-F7

A session created between the `SMEMBERS` read and the `DELETE` survives
deactivation. Closing it properly needs a per-account revocation epoch checked on
every `resolve()`. Today's real guard is identity's sign-in check rejecting a
deactivated account, so this lands *with* identity rather than before it
(FR-IDA-010).

### OI-7 · Password policy is unenforced
**Status:** deferred → identity module · **Raised:** SEC-F8

`MIN_LENGTH`/`MAX_LENGTH` sit in `platform/security/passwords.py` doing nothing.
ASVS L1 wants ≥ 12 characters plus a breached-password check. `FR-IDA` owns the
policy; the constants living unused in platform currently *imply* it is handled.

### OI-8 · No timeout on Valkey calls
**Status:** deferred → needs a value · **Raised:** platform security pass

Postgres has `statement_timeout`; the coordination store has nothing (universal
rule: timeouts on all I/O). Needs a config field, and the *value* matters because
the single-live-call lease sits on the admission path where the participant is
waiting.

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
[data/02 §2](../../../data/02-query-patterns-and-indexes.data.md)). Confirmed
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
**Status:** verify · **Source:** banked DoD item, [quality/08 §4](../../../quality/08-definition-of-done-and-the-build-loop.quality.md), data **F-9**

`platform/queue/enqueue.py` writes `procrastinate_jobs` directly, because
`defer()` manages its own connection and would break the single commit ADR-0023
chose the queue for. The column names are **assumed**. The banked item's own
framing holds: the transactional-enqueue *property* is the commitment and gets an
L3 test; the *syntax* is schematic and gets a version pin.

### OI-10 · Two library assumptions
**Status:** verify

- `valkey.asyncio` import path and pipeline semantics (buffered commands are
  assumed synchronous until `execute()`).
- `meter.create_gauge` needs `opentelemetry-api` ≥ 1.23. Nothing in
  `pyproject.toml` is version-pinned yet.

### OI-11 · Nothing in `platform/` has been executed
**Status:** verify

No tests, no import check, no byte-compile — deliberately, per instruction. The
cross-module wirings (`errors/handlers` → `telemetry/correlation`,
`queue/context` → three others) are verified by reading only.

---

## Cross-repo

### OI-12 · The agent does not match the contract
**Status:** owner-accepted, fix at integration · **Detail:** [call-plane-seam.md](call-plane-seam.md)

`bluelab-agent-prod` calls the retired spec set's endpoints, keys the seam on
`attempt_id` (which cannot serve a test call — FR-DRL-012 creates no attempt),
has no interruption endpoint at all, and streams transcript segments mid-call.
Owner decision: reconcile when connecting the planes.

### OI-13 · The PDF token source crosses a repository boundary
**Status:** owner · **Detail:** [../templates/README.md](../templates/README.md)

ADR-0042 requires one token source for both the SPA and the report PDF, but that
source is `theme.css` in `bluelab-frontend`. How it reaches the template is
undecided — build-time copy, published package, or generated artifact. The
failure mode being avoided is a hand-maintained second palette, because the
report is what goes to HR and defends a hiring decision.

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
