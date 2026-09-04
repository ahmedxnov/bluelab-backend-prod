# bluelab-backend — the application plane and the work plane

**Scaffold only. No implementation yet.** Every package below carries a docstring
naming the requirement, ADR, or design section that governs it, so the structure is
readable against the specification rather than against a convention.

BlueLab is a voice-roleplay platform for sales teams, sold as multi-tenant SaaS: one
AI capability — a simulated buyer on a live voice call, graded against a per-drill
rubric — powering **Training** and **Hiring**, which ship together in v1.

## Why the repository is shaped this way

The system divides into three planes whose runtime characteristics have nothing in
common (architecture/00 §1). **The split is by runtime character, not by domain** —
splitting by domain would distribute data the specification deliberately keeps
together and buy distributed-transaction problems for no return at fifty concurrent
calls (ADR-0001).

| | Application plane | Call plane | Work plane |
|---|---|---|---|
| Unit of work | HTTP request | a live call | a queued job |
| State | none | rich, in-memory, per-call | none |
| Latency contract | ordinary web | **NFR-001 p95 ≤ 1.5 s per turn** | NFR-005 p95 ≤ 45 s |
| Failure of one unit costs | one request | one attempt, void and free | a retry |

**Two of the three live here.** The call session runtime is a separate deployment
unit — [`../bluelab-agent-prod`](../bluelab-agent-prod) — and holds no data-plane
credential (ADR-0071). Its application-plane half lives in `src/bluelab/calls/`.
See [docs/call-plane-seam.md](docs/call-plane-seam.md), which also records a known
divergence between the deployed agent and the contract.

## Layout

```
src/bluelab/
  app.py              the FastAPI factory — composes surfaces, declares no route
  entrypoints/        api · worker · sweeper (one image, several entrypoints)
  api/                route composition only: /api/v1 · /ops/v1 · /hooks · /internal
  modules/            the eight modules of ADR-0002, on the spec ownership lines
    identity/ knowledge/ drills/ review/ training/ hiring/ assessment/ operations/
  calls/              the application plane's half of the call seam (T-1, T-2, T-6)
  work/               the six job lanes of api/02 §2
  notifications/      the email_send ledger and the closed five templates
  adapters/           one file per external capability (C-1, C-6…C-9, C-11, C-13, C-14, C-17)
  platform/           config · ids · clock · db · errors · http · security · telemetry · queue
src/bluelab_runtime_bundle/   the contract package shared with the call plane
sql/                  roles · functions · views (V-1…V-11) · triggers · generated policies
migrations/           Alembic — one linear lineage, one head, forward-only
seeds/                platform reference (data migrations) — the vertical seed is not here
contracts/            generated from api/openapi.yaml and diffed; never authored
tools/                the commit-gate checks that make the discipline executable
tests/                L1 · L2 · L3 · L6 · L7 · L8 · L9   (L4/L5 live with the SPA)
```

Full rationale: [docs/module-boundaries.md](docs/module-boundaries.md).

## The stack, and why

Every line is an ADR, not a preference (stack/00 §3):

| | Choice | ADR |
|---|---|---|
| Language & framework | Python 3.12 + FastAPI + SQLAlchemy 2 + Alembic + Pydantic v2 | 0012 |
| Application-plane shape | modular monolith on the spec ownership lines | 0002 |
| Call-plane isolation | separate unit, no data-plane credential, three signed endpoints | 0071 |
| Transactional store | PostgreSQL 16 **with RLS** | 0022 |
| Scope enforcement | transaction-local GUCs + policies generated from one scope model | 0031 |
| Object store | the S3 **API** is the contract; the implementation is configuration | 0025 |
| Job queue | Procrastinate on the same Postgres — **transactional enqueue** | 0023 |
| Coordination store | Valkey — sessions, leases, registry; volatile by design | 0024 |
| Email | Amazon SES, in-region | 0026 |
| Identity primitives | argon2id, server-side opaque sessions, 256-bit hashed tokens | 0028 |
| Observability | OpenTelemetry, content-free | 0027 |
| Contract | contract-first OpenAPI; the document is authoritative | 0035 |

Python is not a taste call: the call plane is LiveKit Agents, whose lead SDK is
Python, and one backend language beats a TypeScript-app / Python-call split for a
1–3-dev team.

## The five things this codebase must not get wrong

1. **Tenant isolation is structural, not procedural.** Every customer-data table
   carries `org_id` (and almost every one `team_id`); scope arrives as
   transaction-local GUCs; policies are *generated* from one scope model and
   CI-diffed against the live database. Missing context reads as NULL and matches
   nothing — deny by default. The ORM's mandatory scoped-session factory is the
   second belt, and the drift check is what stops the two from ever disagreeing
   silently. `CMP-004`, ADR-0005, ADR-0031.
2. **Concealment has exactly one enforcement point.** The Review module is the sole
   renderer of any scorecard-bearing surface. Authorship lifts concealment; hidden
   motives and challenges are **never** revealed to a non-author, in either product,
   even after the attempt ends. `rubric_breakdown[].weight` is *absent, not null*,
   for non-authors. `FR-SCR-017`, `AC-SCR-003`.
3. **Denial is indistinguishable from non-existence.** Anything outside the
   principal's scope answers 404 with the generic problem — identical in status,
   body bytes, **and timing** to a truly absent id. 403 exists only where the spec
   explicitly discloses state. `AC-IDA-006/007`, ADR-0036.
4. **Frozen content is immutable; erasure is the only writer that crosses.**
   Published drills, frozen assessments, and delivered scorecards are
   trigger-guarded. Erasure removes the person and leaves the statistical residue.
   Freeze and erasure never fight because each has its own mechanism. ADR-0032,
   ADR-0033.
5. **Candidates see no evaluation, ever.** No candidate-authenticated endpoint
   serializes a score, band, scorecard, review, or report field — checkable in the
   OpenAPI document itself, and structural at the database besides. `FR-CND-007`,
   `FR-SCR-018`.

## The invariants, by name

Nine strong-consistency transactions (data/02 §1), each with a concurrency test
proving exactly one racer wins and a replay test proving no duplicate:

`T-1` admission · `T-2` completion hand-off · `T-3` grading write ·
`T-4` knowledge publish · `T-5` drill publish/freeze · `T-6` interruption ·
`T-7` first invite send · `T-8` position close · `T-9` decision & shortlist send.
(`T-10`, erasure and export, is the sanctioned freeze-crossing writer.)

## Getting started

```bash
docker compose up -d
alembic upgrade head
python tools/check_rls_drift.py --apply
```

On a fresh local volume, Compose runs `sql/roles/bootstrap.sql` before PostgreSQL
becomes available: `postgres` remains the container bootstrap superuser,
`bluelab` owns migrations with `BYPASSRLS`, and runtime traffic uses the
non-superuser `bluelab_app` identity from `.env.example`. Set the two database
URLs from that template before running Alembic or the application.

Bring-up from a clean clone must complete in **under 10 minutes**, and that figure
is a timed commit-gate stage, not an aspiration (infra/00 §4).

## Where the truth lives

Nothing in this repository defines behaviour. It implements documents that live in
the repository root and win every disagreement:

| Folder | Answers |
|---|---|
| `specs/` | what the system must do — `FR-*`, `NFR-*`, `CMP-*`, `SEC-*`, and the v1 scope line |
| `architecture/` | the planes, the components, consistency, trust boundaries, capacity |
| `stack/` | which technology, and on what evidence |
| `api/` | the contract — `openapi.yaml` is authoritative |
| `data/` | the schema, the nine transactions, RLS, migrations, backup |
| `security/` | assets, threats, controls, compliance |
| `quality/` | the nine test levels, the bars, the Definition of Done |
| `observability/` | the signal catalogue, SLOs, alerts, the content-free rule |
| `pipeline/` | the commit gate this repository must keep green |
| `infra/` | the environments this repository is deployed into |
| `ux/` | the journeys the API must make reachable |
| `LADDER.md` | which rung the product is on, and what must be true to climb |

A capability is in v1 **if and only if** a requirement in the specification set
states it. There are no endpoints beyond the specification: no document delete, no
drill edit after publish, no self-service org administration, no sixth email.
