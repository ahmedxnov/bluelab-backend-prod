# 02 — Async & Internal Contracts (F-3)

**Establishes:** every interface between planes that is not browser-facing — the agent dispatch payload,
the job catalog with payload schemas and idempotency identities, the email dispatch contract and its
delivery-event ingestion, and the subject-rights execution interfaces. These are contracts in the same
sense as the HTTP surface: Phase 13 builds producers and consumers against them independently. It does
not establish queue technology ([ADR-0023](../stack/adr/0023-job-queue-on-postgres.md)), transaction
bodies ([data 02 §1](../data/02-query-patterns-and-indexes.data.md)), or template prose (Phase 13 with
owner copy).

---

## 1. Agent dispatch payload — application plane → call session runtime

Carried as the explicit-dispatch metadata string in the LiveKit access token
([01 §2](01-realtime-call-contract.api.md)); read by the worker from `JobContext` metadata. JSON, one
schema, versioned by `contract_version` ([04 §4](04-versioning-and-deprecation.api.md)):

```json
{
  "contract_version": 1,
  "call_id": "0197…c4",
  "mode": "attempt",                       // attempt | test
  "attempt_id": "0197…c4",                 // present iff mode=attempt
  "drill_id": "0196…7e",
  "org_id": "0195…11",
  "team_id": "0195…a9",
  "participant": {
    "kind": "rep",                         // rep | candidate | author
    "identity": "acct_0195…d2",            // = LiveKit identity = lease key (ADR-0011)
    "display_name": "…"                    // for logs only; never spoken
  },
  "max_call_seconds": 900,                 // FR-LIV-012 platform maximum
  "reconnect_grace_seconds": 30            // 01 §5
}
```

**Deliberately absent:** drill content, the concealed set, facts, and any prompt material. The session
runtime fetches the **runtime bundle** (§1.1) at session start — one read, then no store access during
turns ([00-overview.arch §3.2](../architecture/00-overview.arch.md),
[ADR-0007](../architecture/adr/0007-frozen-content-as-substrate.md)). Keeping content out of the token
keeps the JWT small and keeps concealed material out of anything a browser transports (the token is
delivered to the client; its metadata must be treated as participant-visible).

**The runtime's writes back *are* an API** ([ADR-0071](../stack/adr/0071-call-plane-process-isolation.md),
2026-07-29 — superseding this section's former "same domain package" position). The call plane holds **no
data-plane credential**: it neither opens a database connection nor executes T-2/T-6 itself. It reports,
over three signed internal endpoints (§1.2), and the application plane executes the transactions. The
**contract** remains the transaction semantics fixed in
[data 02 §1](../data/02-query-patterns-and-indexes.data.md) and the disposition matrix in
[01 §5](01-realtime-call-contract.api.md) — what changed is *which plane runs them*, not what they mean.

### 1.1 The runtime bundle — an allowlist, structurally

`GET /internal/calls/{call_id}/bundle` (HMAC-signed, empty body). The application plane builds it from
the frozen drill; the runtime validates it before starting.

The bundle model declares **no field** for product facts, the rubric, the answer key, or any element of
`drill_concealed` ([data 01 §4](../data/01-schema.data.md)), and forbids unknown keys — so a bundle that
has smuggled forbidden material **fails to parse** rather than reaching prompt assembly. This is the
knowledge boundary as a parse-time guarantee rather than a review-time convention, and it is why
[FR-LIV-009](../specs/13-live-call.spec.md)/[SEC-030](../specs/02-security-requirements.spec.md) are
defensible on a plane that runs model output as control flow. It carries: the frozen scenario, the
participant-safe persona material, language, call type, voice identity, resolved runtime config, the
`call_id`/`org_id`/`team_id` correlation set, `max_call_seconds`, and `reconnect_grace_seconds`.

The v1 JSON shape is closed and versioned:

```json
{
  "contract_version": 1,
  "call_id": "call-…",
  "mode": "attempt",
  "attempt_id": "attempt-…",
  "drill_id": "drill-…",
  "org_id": "org-…",
  "team_id": "team-…",
  "participant": {"kind": "rep", "identity": "acct-…", "display_name": "…"},
  "scenario": {
    "buyer_name": "…", "buyer_role": "…", "buyer_company": "…", "buyer_meta": {},
    "situation_context": "…", "participant_product_summary": "…"
  },
  "persona": {
    "who_you_are": "…", "your_world": "…", "where_you_are_right_now": "…", "call_context": "…"
  },
  "language": "ar-EG",
  "call_type": "discovery",
  "lead_type": "cold_outreach",
  "voice_identity": "…",
  "runtime_config": {
    "stt_provider": "speechmatics",
    "stt_language": "ar_en",
    "stt_operating_point": "enhanced",
    "llm_provider": "anthropic",
    "llm_model": "claude-haiku-4-5-20251001",
    "llm_temperature": 0.8,
    "llm_max_tokens": 1024,
    "llm_prompt_caching": true,
    "tts_provider": "elevenlabs",
    "tts_model": "elevenlabs/eleven_flash_v2_5",
    "turn_detection": "vad",
    "allow_interruptions": true,
    "min_endpointing_delay": 0.4,
    "max_endpointing_delay": 6.0
  },
  "max_call_seconds": 900,
  "reconnect_grace_seconds": 30,
  "prompt_versions": {},
  "model_versions": {}
}
```

`attempt_id` is required iff `mode=attempt` and absent for `mode=test`; `lead_type` is present only
for Discovery. `scenario` is the participant-safe frozen projection shown above, not arbitrary JSON.
Every object forbids unknown keys. Runtime config has exactly the fields shown. It permits Speechmatics
STT (`standard|enhanced`), Anthropic conversational models, ElevenLabs or Azure TTS, and
`vad|stt|multilingual` turn detection. Temperature is 0…1, max tokens 1…4096, delays are non-negative,
and the outer call/reconnect maxima are 900/60 seconds. The application plane resolves any provider
fallback before returning the bundle; the runtime executes the selected config without reaching a
second source of truth.

The bundle **builder** sits in the application plane and is invariant-path code
([quality 01 §4](../quality/01-test-levels-and-quality-bars.quality.md)) — it is the one place a
concealment leak could originate.

### 1.2 The three internal endpoints

Every request carries `X-Agent-Timestamp` (decimal Unix seconds) and `X-Agent-Signature` (lowercase
hex HMAC-SHA256). The signed UTF-8 material is exactly:

```text
METHOD\nPATH\nTIMESTAMP\nsha256(raw_body).hexdigest()
```

`METHOD` is uppercase; `PATH` includes `call_id` and excludes the query string; `TIMESTAMP` is the exact
header text parsed as an integer; and the body digest is computed over the bytes received before JSON
parsing or re-serialization. Verification is constant-time and fails closed for a missing/malformed
header, an invalid signature, or absolute clock skew greater than 300 seconds. This binds an empty-body
bundle request to one call and bounds replay. Completion and interruption are idempotent by `call_id`, so
a valid replay inside the skew window is a domain no-op rather than a second transaction.

| Endpoint | Carries | Application plane runs |
|---|---|---|
| `GET /internal/calls/{call_id}/bundle` | — | Bundle build (§1.1) |
| `POST /internal/calls/{call_id}/completion` | The **whole buffered transcript** with demeanor labels, plus disposition and duration | **T-2, one transaction** — transcript insert → status flip → `grade_attempt` enqueue |
| `POST /internal/calls/{call_id}/interruption` | Disposition + reason | **T-6** — void, allowance reversal, lease release |

Completion body: `{"contract_version":1,"call_id":"…","disposition":"completed",
"duration_seconds":12.3,"transcript":[...]}`. Each transcript item is
`{sequence_index,speaker,text,timestamp_seconds,demeanor_label}` and the array is ordered by
`sequence_index`. `demeanor_label` is one coarse string or `null`, and is `null` for prospect rows,
matching the canonical `transcript_entry.demeanor_label` column. Interruption body:
`{"contract_version":1,"call_id":"…","disposition":"interrupted|never_established","reason":"…"}`.
The path `call_id` and body `call_id` must match. Unknown keys or unsupported dispositions fail closed.

**The transcript crosses once.** It is buffered in session memory for the whole call and delivered with
completion, never streamed per segment — per-segment delivery would break T-2's single transaction and
strand orphan segments behind every interrupted call
([FR-LIV-015](../specs/13-live-call.spec.md) void-and-free).

**Classification stays with the runtime; the write stays with the application plane.** The runtime is
still the single authority on *how a call ended* ([01 §5](01-realtime-call-contract.api.md)) — only it has
the facts. It reports that decision instead of executing it.

**A lost completion request is a real failure mode**, so the lease-expiry sweep and the
`POST /hooks/livekit` reconciliation ([01 §7](01-realtime-call-contract.api.md)) are load-bearing here,
not theoretical: they are what guarantee no attempt is stranded `in_progress`.

## 2. Job catalog — application/call plane → work plane

At-least-once queue, idempotent consumers, transactional enqueue (job insert commits with the domain
write — [ADR-0023](../stack/adr/0023-job-queue-on-postgres.md)). Payloads carry **ids only, never
content** — every worker re-reads the request-bound captured or frozen truth from the store, which is
what makes retries safe.

| Job (queue name) | Payload | Idempotency identity | Enqueued by | Retry / failure surface |
|---|---|---|---|---|
| `grade_attempt` | `{attempt_id}` | `scorecard.attempt_id` unique — T-3 insert `ON CONFLICT DO NOTHING`; a second grade is a silent no-op ([FR-SCR-003](../specs/14-scoring-and-review.spec.md)) | T-2 (call completion — this insert **is** the completion event, [00-overview.arch §4](../architecture/00-overview.arch.md)) | Backoff retry; while failing: `attempt.status='grading_pending'` (participant reads *preparing*); on exhaustion insert `ops_fault(kind='grading_failure')` — never voids the call ([FR-SCR-009](../specs/14-scoring-and-review.spec.md)) |
| `generate_scenario` | `{drill_id, request_id}` | `request_id` (one per author click); the worker reads the stored inputs and `draft_grounding`, then writes only while the drill still names that running scenario request | `POST /drills/{id}/scenario-generation` through T-5a | On exhaustion: matching request becomes `failed` with an author-safe reason; **no fallback content**; publish stays blocked ([FR-DRL-006](../specs/12-drill-lifecycle.spec.md)). Retry = author re-request |
| `generate_rubric` | `{drill_id, request_id}` | as above; the worker reads the stored scenario, inputs, and the same `draft_grounding`, then atomically installs the complete validated rubric for the matching request | `POST /drills/{id}/rubric-generation` through T-5b | As `generate_scenario`; the accepted request discards the prior rubric, so failure exposes no fallback ([FR-DRL-011](../specs/12-drill-lifecycle.spec.md)) |
| `extract_facts` | `{upload_id}` | `document_upload.status` transition `received→extracting→extracted|failed`; re-run of a terminal upload is a no-op | `POST /product-documents/{id}/uploads` | On failure/empty: `status='failed'` + reason; nothing reaches review; live facts untouched ([FR-KNW-008](../specs/11-knowledge.spec.md)). Retry = new upload POST re-running extraction on the same stored object |
| `render_report` | `{candidate_id}` | `candidate_report` upsert keyed on `candidate_id`; PDF render conditional on `pdf_status in (none,failed)` | Candidate completion (T-2 candidate variant); `POST /candidates/{id}/report/render` (manager retry when `failed`) | On failure: `pdf_status='failed'`; report view still renders from data ([FR-HIR-011](../specs/22-hiring-manager.spec.md)); takeaway synthesis via C-6 inside this job ([00-overview.arch §3.3](../architecture/00-overview.arch.md)) |
| `dispatch_email` | `{email_send_id}` | `email_send (kind, dedupe_key)` unique — at most one send per (recipient, event) ([04-deps §2.6](../architecture/04-dependencies-and-capabilities.arch.md)) | T-7 (invites), T-9 (shortlist), account provisioning, completion/decision events per policy | Backoff retry; terminal failure sets `email_send.status='failed'`; E-2 delivery state feeds the pipeline view ([FR-HIR-010](../specs/22-hiring-manager.spec.md)) |
| `execute_erasure` | `{request_id}` | `erasure_request.id`; a write-once replay marker arms the request before mutation, the database procedure is idempotent over the erasure matrix, object deletes are idempotent by exact key, and zero-remain verification converges on replay | Accepted `pending` request with its source-data restriction; conditional status claim and marker check exclude a terminal operator decision | Sets `processing`; retryable failure leaves `failed` with its fence and restriction in force; verified success and ordered restriction release set `executed`, `executed_at`, and per-category evidence ([SEC-018](../specs/02-security-requirements.spec.md)) |
| `execute_export` | `{request_id}` | `export_request.id` and the fixed object key `exports/{request_id}.zip`; assembly replaces that exact object and the ready transition is conditional on `processing` | Accepted `pending` request with its source-data restriction | Sets `processing`; retryable failure leaves `failed` and the restriction in force; success sets `ready`, `ready_at`, `expires_at`, and the fixed bundle key until delivery obligations resolve |

The job catalogue above contains eight contracted job kinds. The organization term-transition job is
enabled with service-date access enforcement. The organization-purge jobs remain disabled until the
[offboarding enablement conditions](../data/03-lifecycle-retention-erasure.data.md) pass:

| Job | Payload | Cadence and identity | Failure contract |
|---|---|---|---|
| `transition_due_org_terms` | `{}` | Hourly set-based scan of current service terms whose persisted end instant has passed; one stable transition per `(org_id, service_term_id)` | A missed scan is retried; the expired term remains discoverable. The service-date gate refuses ordinary access throughout any delay. |
| `discover_org_purges` | `{}` | Hourly set-based scan; one queued job per eligible `(org_id, offboarding_id)` | A missed scan is retried; eligible episodes remain discoverable. |
| `execute_org_purge` | `{org_id, offboarding_id}` | One durable run per episode; each bounded step uses its run, step, and batch identity | Restriction pauses further authorization; transient failures back off from five minutes to one hour; five failures or 24 hours without progress requires attention. |
| `recover_org_purges` | `{}` | Every 15 minutes, independently scans incomplete runs, including runs with no live organization row | Re-inventory uncertain batches and reconcile independent evidence before resuming. |

Procrastinate is the sole organization-lifecycle scheduler and purge executor: term transition records an
episode from the persisted service end rather than execution time; set-based discovery selects eligible
offboarding episodes; an organization-specific job carries the stable organization and episode
identifiers; claim rechecks current deadline, restrictions, permitted work, and pending lifecycle
decisions; incomplete purge runs remain discoverable for recovery. The concrete queue
payloads and step identities above follow the version-N/N+1 compatibility rule. Ordinary
workers enforce the organization service-date and lifecycle gates at their write boundary and reject work
outside the current term or for an offboarding or purging organization; narrowly authorized hold-period and
purge work uses guarded paths.

Worker concurrency, queue depth alarms, and dead-letter handling are Phase 7/11 concerns; the contract
here is payload + identity + failure surface.

The physical generation state is two stage-specific state pairs on `drill`:
`scenario_generation_status`/`scenario_generation_request_id` and
`rubric_generation_status`/`rubric_generation_request_id`, with one shared `generation_error`
([data 01 §4](../data/01-schema.data.md)). A database constraint permits at most one running stage. The HTTP
`GenerationState` projection reads the two statuses directly. Replacing inputs resets both stages and
invalidates both outputs; requesting scenario generation sets the scenario stage to `running` and resets the
rubric stage; requesting rubric generation requires a succeeded scenario and sets only the rubric stage to
`running`. `error` carries the bounded safe reason for the failed stage and is `null` otherwise. Request
identities remain internal. Every displaced worker sees a stage-specific request mismatch and completes as a
no-op.

E-1 issuance atomically persists the credential or reset-token hash, `email_send`, its
`email_delivery_secret` authenticated-encryption envelope, and the `{email_send_id}` job. The dispatcher
resolves the account and envelope from that id, verifies the envelope's bound send id, org id, and purpose,
and decrypts it only while rendering E-1. An absent or expired envelope is a terminal delivery failure. The
dispatcher deletes the envelope after transport acceptance or terminal failure; the expiry sweep removes any
stranded envelope. Plaintext never enters a job payload, dispatch ledger, log, metric, or trace.

## 3. Email contract — the closed five

The dispatcher renders exactly five templates and **rejects any `kind` outside the inventory**
([00 §6](../specs/00-overview.spec.md) is closed; [00-overview.arch §3.3](../architecture/00-overview.arch.md)).
Template prose is owner-supplied at Phase 13; the contract fixes trigger, recipient resolution, dedupe
key, and required variables:

| Kind | Trigger | Recipient | Dedupe key | Required variables |
|---|---|---|---|---|
| `E1_credentials` | Account provisioned ([FR-IDA-003](../specs/10-identity-and-access.spec.md)); password-reset request ([FR-IDA-006](../specs/10-identity-and-access.spec.md)) | Account email | account id (provisioning) / reset-token id (reset) | recipient name, sign-in URL, initial credential **or** time-limited reset link |
| `E2_invite` | Invite send / resend (T-7, [FR-HIR-008](../specs/22-hiring-manager.spec.md)/[009](../specs/22-hiring-manager.spec.md)) | Candidate email | token id — a resend is a new token, hence a new send; the prior token's email is never re-sent | candidate name, position title, org name, **invite URL with token in fragment**, expiry date, editable body ([position.invite_template]) |
| `E3_candidate_report` | Completion (policy `after_finish`) or reject decision (policy `rejected_only`); never under `withhold` ([FR-CND-012](../specs/23-candidate-flow.spec.md)) | Candidate email | candidate id | candidate name, position title, **concealment-safe PDF attached** ([FR-HIR-012](../specs/22-hiring-manager.spec.md)) |
| `E4_shortlist` | Shortlist send (T-9, [FR-HIR-014](../specs/22-hiring-manager.spec.md)) | Resolved recipient list as sent (snapshot on `shortlist.recipients`) | shortlist id | manager-edited body, per-candidate PDFs attached |
| `E5_completion` | Candidate completes and position toggle on ([FR-HIR-017](../specs/22-hiring-manager.spec.md)) | Owning manager's email | candidate id | candidate name, position title, pipeline URL |

**Delivery events:** the email provider's delivery/bounce/delay notifications are ingested (SES event
destination, [ADR-0026](../stack/adr/0026-email-ses.md)) and update `email_send.status`
(`sent → delivered | bounced | delayed`). Ingestion is provider-authenticated per the provider's
signing mechanism, idempotent per `provider_message_id`, and is deployment configuration — not a product
HTTP endpoint in [openapi.yaml](openapi.yaml); Phase 7 owns its wiring. The **product-visible** result is
one field: the invite delivery state on the pipeline ([FR-HIR-010](../specs/22-hiring-manager.spec.md) —
delivered-vs-ignored honesty).

## 4. Report and PDF rendering constraints

The `render_report` worker may read: the candidate row (minus nothing — it is team-scoped), graded
attempts with scorecards/moments, the position, and the drills' **participant-safe projection only** for
per-drill cards. It must not read `drill_concealed` or rubric weights — the PDF is concealment-safe **by
construction**, not by post-filtering ([FR-HIR-012](../specs/22-hiring-manager.spec.md),
[02-trust §6](../architecture/02-trust-and-access.arch.md)); the manager's `internal_note` is excluded
the same way. One artifact serves HR and the candidate. Commentary English, quotes verbatim Arabic with
correct RTL rendering (C-9 obligation, [ADR-0021](../stack/adr/0021-pdf-chromium-playwright.md)).

## 5. Subject-rights execution — erasure and export

Triggered only from the ops surface (`/ops/v1/erasure-requests`, `/ops/v1/export-requests` —
[openapi.yaml](openapi.yaml)); executed by dedicated subject-rights procedures, with the
organization-purge batch path authorized separately ([ADR-0033](../data/adr/0033-erasure-anonymize-redact-delete.md),
[data 03 §3–4](../data/03-lifecycle-retention-erasure.data.md)). The interface contract: requests carry
`{org_id, subject_kind, subject_id, reason}`; status is polled on the request resource
(`pending → processing → executed|failed` for erasure; export additionally reaches `ready` and
`delivered`; `awaiting_input`, `rejected`, and `withdrawn` follow `subject-rights:v1` in
[data 03 §6](../data/03-lifecycle-retention-erasure.data.md)); evidence
counts land on the request row. Erasure reaches the object store (recordings, PDFs, bundles) as well as
rows — [CMP-001](../specs/01-nfr-and-compliance.spec.md)'s full reach.

Acceptance of either request that needs source data records an organization-scoped deletion restriction
in the lifecycle history, including while the organization has no offboarding episode. A stable
operation identity resolves uncertain acceptance. The accepted event projects the request,
restriction, audit record, and job enqueue in one application transaction; the response follows
successful projection. Pending operations fence purge claim and destructive authorization, and the
job cannot run before its restriction is applied. An `awaiting_input` request has
a recorded response deadline and remains restricted until an authorized, audited closure.

The accepted restriction event carries the request kind, request and restriction identifiers,
organization and subject identifiers, request-policy reference, acceptance time, operator and audit
identifiers, sanitized reason, initial state, restriction scope, authority reference and release
condition, and request-keyed job identity. Replay
projects the request, restriction, audit record, and one job enqueue from that protected event.
Nonterminal state decisions use an ordered `set_subject_request_state` event. Terminal request
decisions and restriction release share one `release_restriction` operation identity and projection;
release addresses the organization and restriction, including when no episode exists.
An erasure request in `pending`, `processing`, `awaiting_input`, or `failed` fences subject-related
grading, report, generation, and email work. An armed replay marker makes that fence permanent.
An unarmed `rejected` or `withdrawn` request stops fencing only after ordered restriction release.
`execute_erasure` writes the request's replay marker to the
independent compliance ledger before it invokes the database procedure, waits for or cancels already-claimed
subject work, deletes the exact object keys returned by the procedure, performs category-by-category
zero-remain verification, and projects an ordered restriction release with the executed state and
evidence. A ledger-write failure leaves the request
pending and permits no erasure mutation. Restore reconciliation recreates missing request rows from the
ledger when their organization remains live. When a verified organization purge removes the organization,
independent evidence records the subject-erasure outcome for its proven overlapping scope and remaining
obligations still execute. Joint replay finishes before ordinary workers resume
([data 05 §4](../data/05-backup-and-disaster-recovery.data.md)).
