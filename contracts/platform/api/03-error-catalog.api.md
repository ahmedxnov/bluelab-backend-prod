# 03 — Error Catalog

**Establishes:** the error model every interface family uses and the complete registry of problem types —
each with its status, producing conditions, and retry semantics. Endpoint-to-problem wiring lives in
[openapi.yaml](openapi.yaml); the design decision is [ADR-0036](adr/0036-problem-details-denial-semantics.md).

---

## 1. The error shape — RFC 9457 Problem Details

Every non-2xx response from `/api/v1`, `/ops/v1`, and `/hooks/*` is `application/problem+json`:

```json
{
  "type": "/problems/allowance-exhausted",
  "title": "Attempts allowance exhausted",
  "status": 409,
  "detail": "This assigned drill has used 3 of 3 allowed attempts.",
  "request_id": "req_0197f3…",
  "meta": { "attempts_used": 3, "attempts_allowed": 3 }
}
```

- **`type`** — stable, origin-relative URI from the registry below; **the machine contract**. Clients
  branch on `type` (or `status` for generic handling), never on `detail` text.
- **`title`** — fixed English per type. **`detail`** — human-readable, English, safe to show; contains
  no concealed material and no cross-scope information, ever.
- **`request_id`** — correlation id, present on every response (also echoed as `X-Request-Id` header).
- **`meta`** — typed per-problem extension object; documented per type below. Validation problems carry
  `errors[]` instead: `[{ "field": "candidates[2].email", "message": "…" }]`.
- Resolvability: `GET /problems/{slug}` serves the type's documentation page; the URI is the identifier
  either way.

Two semantics rules from [00 §4](00-contract-overview.api.md), restated because they shape the registry:
**cross-scope denial is `not-found`** (identical to absence — no `forbidden` problem exists for customer
data), and **participant-facing grading failure is not an error** (a `grading_pending` attempt is a
successful `200` whose status field reads as preparing, [FR-SCR-009](../specs/14-scoring-and-review.spec.md)).

## 2. Registry

### 2.1 Generic (any endpoint)

| `type` slug | Status | When | Retry? |
|---|---|---|---|
| `validation-error` | 422 | Request shape/type/constraint violations; carries `errors[]` | After fixing input |
| `malformed-request` | 400 | Unparseable body, unknown content type, malformed cursor | No |
| `not-found` | 404 | Absent resource **or any resource outside the caller's scope** ([AC-IDA-006](../specs/10-identity-and-access.spec.md)/[007](../specs/10-identity-and-access.spec.md)) — indistinguishable | No |
| `method-not-allowed` | 405 | Wrong verb on a known path | No |
| `rate-limited` | 429 | Throttle tripped ([05](05-rate-limits-and-quotas.api.md)); `Retry-After` header always present | After `Retry-After` |
| `internal-error` | 500 | Unhandled fault; `request_id` is the support handle | Yes, idempotent ops only |
| `service-unavailable` | 503 | Dependency outage or maintenance; `Retry-After` when known | After `Retry-After` |

### 2.2 Authentication & session

| `type` slug | Status | When | Notes |
|---|---|---|---|
| `invalid-credentials` | 401 | Sign-in with wrong email/password — **identical whether the account exists or not** ([FR-IDA-005](../specs/10-identity-and-access.spec.md)) | Counted toward auth throttle |
| `account-deactivated` | 403 | Sign-in with **correct** credentials on a deactivated account — the one state the spec discloses ([AC-IDA-003](../specs/10-identity-and-access.spec.md)) | |
| `session-invalid` | 401 | Missing/expired/revoked session cookie on an account or ops endpoint | SPA routes to sign-in |
| `first-sign-in-required` | 409 | Gate-limited session calls anything but the gate ([FR-IDA-004](../specs/10-identity-and-access.spec.md)) | SPA routes to the gate screen |
| `first-sign-in-not-pending` | 409 | `POST /auth/first-sign-in` on an account whose credential is already set — the gate is not pending (gate F-6) | Use `POST /auth/acceptances` for later re-consent |
| `consent-required` | 409 | Recording consent missing for current notice version — at the gate endpoints, and as the universal call backstop ([CMP-002](../specs/01-nfr-and-compliance.spec.md), [FR-LIV-004](../specs/13-live-call.spec.md), [AC-LIV-007](../specs/13-live-call.spec.md)) | `meta.notice_version` |
| `terms-acceptance-required` | 409 | Terms/privacy acceptance missing for current versions ([CMP-005](../specs/01-nfr-and-compliance.spec.md)) | `meta.terms_version`, `meta.privacy_version` |
| `reset-token-invalid` | 401 | Password-reset completion with an expired/used/unknown token ([FR-IDA-006](../specs/10-identity-and-access.spec.md)) | |
| `password-policy` | 422 | New password below policy (Phase 8 owns the parameters) | `errors[]` |

### 2.3 Candidate token

| `type` slug | Status | When | Notes |
|---|---|---|---|
| `token-invalid` | 401 | Unknown/revoked token — revoked (position closed, [FR-HIR-015](../specs/22-hiring-manager.spec.md)) is indistinguishable from unknown | |
| `token-expired` | 401 | Past expiry, unused or mid-assessment — **with the expiry explanation** the spec requires ([FR-IDA-013](../specs/10-identity-and-access.spec.md), [AC-IDA-004](../specs/10-identity-and-access.spec.md), [FR-CND-011](../specs/23-candidate-flow.spec.md)) | `meta.expired_at` |
| `assessment-completed` | 409 | A completed candidate's token calls anything except `GET /assessment` (which serves the completion state, [FR-IDA-013](../specs/10-identity-and-access.spec.md), [FR-CND-008](../specs/23-candidate-flow.spec.md)) | |
| `preflight-required` | 409 | Stage/call access before pre-flight passed + consent + terms ([FR-CND-002](../specs/23-candidate-flow.spec.md)/[003](../specs/23-candidate-flow.spec.md)) | `meta.missing` lists failed elements |

### 2.4 Call admission & call plane

| `type` slug | Status | When | Notes |
|---|---|---|---|
| `call-already-active` | 409 | Second start while a live call exists for the participant — any session, device, or token ([FR-LIV-005](../specs/13-live-call.spec.md), [AC-LIV-003](../specs/13-live-call.spec.md)/[008](../specs/13-live-call.spec.md)); also `DELETE /calls/current` on an established call ([01 §3](01-realtime-call-contract.api.md)) | Active call is left undisturbed |
| `allowance-exhausted` | 409 | Assigned drill's attempts allowance used up ([FR-TRP-013](../specs/20-training-rep.spec.md), [AC-TRP-005](../specs/20-training-rep.spec.md)) | `meta.attempts_used/attempts_allowed` |
| `stage-not-next` | 409 | Candidate requests a stage out of the frozen order ([FR-CND-004](../specs/23-candidate-flow.spec.md), [AC-CND-002](../specs/23-candidate-flow.spec.md)) | `meta.next_stage_ord` |
| `stage-consumed` | 409 | Second interruption already consumed the stage ([FR-CND-010](../specs/23-candidate-flow.spec.md)) | |
| `drill-not-startable` | 409 | Call requested on a draft (non-author), archived, or otherwise untakeable drill ([FR-DRL-013](../specs/12-drill-lifecycle.spec.md)/[016](../specs/12-drill-lifecycle.spec.md)) | |
| `no-active-call` | 404 | `DELETE /calls/current` with nothing to cancel | Safe to ignore |
| `review-not-ready` | 409 | Review fetched before grading completed — the attempt exists and is being prepared ([FR-SCR-009](../specs/14-scoring-and-review.spec.md)); keep polling the attempt | Poll `GET /attempts/{id}` |
| `call-capacity` | 503 | Admission refused at concurrency limit ([NFR-002](../specs/01-nfr-and-compliance.spec.md) protection) — failure-to-establish semantics: nothing recorded or consumed, retry offered ([FR-LIV-016](../specs/13-live-call.spec.md)) | `Retry-After` |

### 2.5 Authoring & knowledge

| `type` slug | Status | When | Notes |
|---|---|---|---|
| `not-job-relevant` | 422 | Custom challenge/motive rejected by the job-relevance gate, **with the reason, before it influences generation** ([FR-DRL-007](../specs/12-drill-lifecycle.spec.md), [AC-DRL-004](../specs/12-drill-lifecycle.spec.md), [CMP-003](../specs/01-nfr-and-compliance.spec.md)) | `errors[]` per rejected entry with reason |
| `generation-incomplete` | 409 | Publish without successfully generated scenario **and** rubric ([FR-DRL-006](../specs/12-drill-lifecycle.spec.md)) | |
| `generation-in-progress` | 409 | Generation requested while one is running for the same drill | Poll the drill |
| `weights-not-100` | 409 | Publish while rubric weights ≠ 100 ([FR-DRL-010](../specs/12-drill-lifecycle.spec.md)) | `meta.delta` — the "+3 to balance" number ([AC-DRL-003](../specs/12-drill-lifecycle.spec.md)) |
| `drill-not-draft` | 409 | Input/rubric mutation or publish on a published/archived drill ([FR-DRL-015](../specs/12-drill-lifecycle.spec.md)) | |
| `drill-not-archivable` | 409 | Archive on a draft, or by a non-manager | |
| `document-cap` | 409 | Eleventh document ([FR-KNW-001](../specs/11-knowledge.spec.md)) | `meta.cap: 10` |
| `file-too-large` | 413 | Upload > 20 MB ([FR-KNW-003](../specs/11-knowledge.spec.md)) — refused at selection with the reason | `meta.max_bytes` |
| `unsupported-file-type` | 415 | Upload outside the supported document types ([FR-KNW-003](../specs/11-knowledge.spec.md)); also the candidate bulk-parse template mismatch ([FR-HIR-007](../specs/22-hiring-manager.spec.md)) | `meta.supported` |
| `no-draft-to-review` | 409 | Review/publish with no pending replacement | |
| `stale-review` | 409 | Publish whose `based_on_version` lost the CAS — live facts changed since the review was built ([FR-KNW-011](../specs/11-knowledge.spec.md), [AC-KNW-005](../specs/11-knowledge.spec.md)) | Client refetches `/review`, re-confirms |
| `extraction-pending` | 409 | Review requested while extraction is still running | Poll the document |

### 2.6 Hiring

| `type` slug | Status | When | Notes |
|---|---|---|---|
| `assessment-frozen` | 409 | Assessment edit after first invite ([FR-HIR-005](../specs/22-hiring-manager.spec.md), [AC-HIR-001](../specs/22-hiring-manager.spec.md)) | |
| `assessment-empty` | 409 | Invite send with zero stages | |
| `position-closed` | 409 | Any mutation on a closed position ([FR-HIR-015](../specs/22-hiring-manager.spec.md)) | Read surfaces stay open |
| `decision-frozen` | 409 | Decision change after shortlist inclusion ([FR-HIR-013](../specs/22-hiring-manager.spec.md), [AC-HIR-005](../specs/22-hiring-manager.spec.md)) | |
| `candidate-not-decidable` | 409 | Decision on a candidate with no completed/incomplete evidence yet | |
| `shortlist-empty` | 409 | Shortlist send with no includable (approved, unsent) candidates ([FR-HIR-014](../specs/22-hiring-manager.spec.md)) | |
| `pdf-not-ready` | 409 | PDF fetch while `pdf_status` ∈ `none|pending|failed` ([FR-HIR-012](../specs/22-hiring-manager.spec.md)) | `meta.pdf_status` |
| `idempotency-key-reuse` | 409 | Same `Idempotency-Key`, different body ([ADR-0040](adr/0040-idempotency-strategy.md)) | |
| `idempotency-key-required` | 422 | Batch/send POST without the header — a validation failure like any other missing required element (gate F-5) | |

### 2.7 Ops surface

| `type` slug | Status | When | Notes |
|---|---|---|---|
| `ops-session-invalid` | 401 | Missing/expired ops session | Separate from customer `session-invalid` — distinct cookie, distinct surface ([ADR-0010](../architecture/adr/0010-operations-plane-explicit.md)) |
| `manager-owns-dependents` | 409 | Deactivating a manager who still owns reps or open positions ([FR-IDA-010](../specs/10-identity-and-access.spec.md)) | `meta.reps`, `meta.open_positions` counts |
| `reason-required` | 422 | Any ops mutation without `reason` (audit obligation, [ADR-0010](../architecture/adr/0010-operations-plane-explicit.md)) | |
| `duplicate-email` | 409 | An email already exists in the target container — ops provisioning (platform-wide username, [data 01 §2](../data/01-schema.data.md)) and `POST /hr-contacts` (already in the manager's saved list). One condition class, one type (gate F-8) | |
| `fault-already-resolved` | 409 | Resolving a resolved fault | |
| `subject-unknown` | 404 | Erasure/export subject id not found in the named org | |

### 2.8 Integration

| `type` slug | Status | When |
|---|---|---|
| `webhook-signature-invalid` | 401 | `/hooks/livekit` request whose signed JWT fails validation ([01 §7](01-realtime-call-contract.api.md)) |

## 3. Registry discipline

- A new problem type is a contract change: added here first, then wired in the spec — additive under
  [04](04-versioning-and-deprecation.api.md); **repurposing or renaming a slug is breaking** and does not
  happen inside v1.
- One condition, one type: the same domain refusal never surfaces under two slugs on different endpoints.
- No endpoint may emit a problem outside this registry (CI checks the spec's declared responses against
  it — [ADR-0035](adr/0035-contract-first-openapi.md)).
