# 06 — FR Coverage Matrix

**Establishes:** the reachability proof the phase's gate walks — every functional requirement mapped to
the contract element(s) that realize it, plus the NFR/CMP touchpoints the contract itself carries. An FR
with no row here would be unreachable through the specified interfaces — a Phase 5 defect by definition.

Notation: bare paths are `openapi.yaml` operations (prefix `/api/v1` omitted; ops rows keep `/ops/v1`);
**F-2** = [01-realtime-call-contract](01-realtime-call-contract.api.md); **F-3** =
[02-async-internal-contracts](02-async-internal-contracts.api.md); problem slugs are
[03-error-catalog](03-error-catalog.api.md) types. "SPA" marks the client-side half of a shared
obligation, stated so the gate can check nothing silently fell between contract and client.

---

## FR-IDA — Identity & Access

| FR | Realized by |
|---|---|
| FR-IDA-001 | `POST /ops/v1/accounts` (roster entry {email, role, org, manager}) |
| FR-IDA-002 | No self-registration endpoint exists on any surface — checkable property of the spec |
| FR-IDA-003 | E-1 dispatch on provisioning (F-3 §3, dedupe per account) |
| FR-IDA-004 | Gate-limited session ([00 §3](00-contract-overview.api.md)) + `POST /auth/first-sign-in` (all three actions, one call; `409 first-sign-in-required` on everything else) |
| FR-IDA-005 | `POST /auth/session` — uniform `401 invalid-credentials` (existence never disclosed); role-scoped surfaces via session security on every endpoint |
| FR-IDA-006 | `POST /auth/password-reset-request` (always 202) + `POST /auth/password-reset` (single-use, `401 reset-token-invalid`); E-1 |
| FR-IDA-007 | `SessionView.role` selects surfaces; manager/rep endpoints 404 across roles per scope model |
| FR-IDA-008 | Scope tuple resolution on every request ([00 §3](00-contract-overview.api.md)); no org parameter exists anywhere on the customer surface — org is ambient, never client-chosen |
| FR-IDA-009 | Team-scoping of every team surface (library, knowledge, catalog, positions — [00 §4](00-contract-overview.api.md)); mapping changes only via `POST /ops/v1/accounts/{id}/team-change` |
| FR-IDA-010 | `POST /ops/v1/accounts/{id}/deactivate` (`409 manager-owns-dependents` until reassignment); sign-in `403 account-deactivated`; instant session revocation (ADR-0037); history preserved (no delete surface exists) |
| FR-IDA-011 | `POST /positions/{id}/invites` issues bound tokens (T-7); bearer scheme `candidateToken`; binding validated per request (ADR-0037) |
| FR-IDA-012 | Token validation window on every `/assessment*` + `/calls` call; resend keeps prior token valid (AC-IDA-005 — many-tokens→one-candidate validation set) |
| FR-IDA-013 | Completed: every candidate endpoint except `GET /assessment` answers `409 assessment-completed`; `GET /assessment` serves only `completion`. Expired: `401 token-expired` with explanation |

## FR-KNW — Knowledge

| FR | Realized by |
|---|---|
| FR-KNW-001 | `GET/POST /product-documents` (`409 document-cap` at 10) |
| FR-KNW-002 | `FactInput`/fact rows: label, value, optional note |
| FR-KNW-003 | `POST /product-documents/{id}/uploads` (≤ 20 MB `413`, types `415`, 202 → extraction; live facts untouched) |
| FR-KNW-004 | `PUT /product-documents/{id}/draft` (prefill = client GET of live facts; row edit/remove/add; same review step) |
| FR-KNW-005 | `GET /product-documents/{id}/review` (`ReviewDiff`: changed/added/removed with priors + explicit replace warning) |
| FR-KNW-006 | `POST /product-documents/{id}/publish` (T-4 atomic swap; exactly one live set by construction) |
| FR-KNW-007 | Generation grounds in live facts (F-3 §2 `generate_*`); publish freezes the snapshot (T-5); grading and reference read the snapshot (`GET .../reference` `frozen: true`); no cross-team source can be expressed — team scope is ambient |
| FR-KNW-008 | Upload/extraction failure → `latest_upload.status: failed` + reason + retry; nothing reaches review |
| FR-KNW-009 | `GET /drills/{id}/reference` + `GET /assessment/stages/{id}/reference` (`ReferenceView`: grouped facts, provenance line, zero mutation affordance in the schema) |
| FR-KNW-010 | `DocumentSummary.draft` status marker while live facts keep serving |
| FR-KNW-011 | `based_on_version` CAS on publish → `409 stale-review`, client rebuilds review (AC-KNW-005) |

## FR-DRL — Drill Lifecycle

| FR | Realized by |
|---|---|
| FR-DRL-001 | `CallType`/`LeadType` schemas; lead_type iff discovery (422 otherwise) |
| FR-DRL-002 | `POST /drills` + `PUT /drills/{id}/inputs` (library `option_id` / custom `label` entries; `GET /authoring-options`); generation 422 without ≥1 challenge and ≥1 motive |
| FR-DRL-003 | `language` fixed `ar-EG` in every drill schema — not writable |
| FR-DRL-004 | `POST /drills/{id}/scenario-generation` → `ScenarioView` (persona identity, context, references from published facts) + persona-derived `label` |
| FR-DRL-005 | `ScenarioView` is read-only (no scenario-mutation endpoint exists); regeneration replaces wholesale |
| FR-DRL-006 | `generation.{scenario,rubric}_status: failed` + `error` + re-request retry; publish blocked `409 generation-incomplete`; no fallback content path exists |
| FR-DRL-007 | `422 not-job-relevant` with per-entry reasons at input commit — before generation (AC-DRL-004) |
| FR-DRL-008 | `POST /drills/{id}/rubric-generation` → `RubricView` (name, weight, rationale per dimension) |
| FR-DRL-009 | `PATCH /drills/{id}/rubric/weights` + `DELETE /drills/{id}/rubric/dimensions/{id}`; live `total` returned; no add/edit-name/edit-rationale surface exists (AC-DRL-008) |
| FR-DRL-010 | `POST /drills/{id}/publish` → `409 weights-not-100` with `meta.delta` (AC-DRL-003) |
| FR-DRL-011 | `rubric-generation` requires `discard_confirmed: true` over an existing rubric |
| FR-DRL-012 | `POST /calls` with `mode: test` — same live call, `attempt_id: null`, no capture, no record (F-2 §5 disposition; AC-DRL-005) |
| FR-DRL-013 | Drafts are unpublished drills: `POST /drills` at any completeness, resume via `GET /drills/{id}`; listed only in their author's authoring surface — manager drafts in `GET /team/drills`, rep drafts in `/me/library` Created-by-me marked `status: draft` (gate F-1); no other principal's surface lists them and none can be taken (`409 drill-not-startable`) |
| FR-DRL-014 | `POST /drills/{id}/publish` — the T-5 freeze (scenario, label, rubric, language, answer-key snapshot) |
| FR-DRL-015 | No mutation endpoint accepts a published drill (`409 drill-not-draft` everywhere); freeze guards behind (data 01 §11) |
| FR-DRL-016 | `POST /drills/{id}/archive` — withdrawal (library/assessment/assignment) with history preserved (AC-DRL-007) |

## FR-LIV — Live Call

| FR | Realized by |
|---|---|
| FR-LIV-001 | `GET /drills/{id}/brief` + `GET /assessment/stages/{id}/brief` (`BriefView` from frozen scenario, call-type-appropriate content) |
| FR-LIV-002 | `BriefView.author_recap` — present only for the author, absent otherwise (AC-LIV-002) |
| FR-LIV-003 | SPA journey: the only start-call control renders on the brief/reference; server enforces eligibility at `POST /calls` (T-1) |
| FR-LIV-004 | `409 consent-required` at admission — the universal backstop (AC-LIV-007) |
| FR-LIV-005 | Participant-keyed lease at admission → `409 call-already-active` across sessions/devices/tokens (AC-LIV-003/008); `DELETE /calls/current` cannot touch an established call |
| FR-LIV-006 | F-2 §4.2: `lk.agent.state` attribute (buyer state), local `IsSpeakingChanged` (participant), SPA renders status/elapsed/identity-recap chrome from `CallGrant` + brief data |
| FR-LIV-007 | F-2 §4.1: `lk.transcription` text streams (final/interim, speaker by identity); focus-mode client-side with capture unaffected (AC-LIV-004) |
| FR-LIV-008 | F-2 §4.2: local microphone track mute, state visible client-side |
| FR-LIV-009 | Agent session in persona (Phase 13 implements); concealment carried in agent instructions — F-2 §9, threat-modeled Phase 8 |
| FR-LIV-010 | F-2 §6: in-session transcript+demeanor capture → T-2 bulk insert (complete at call end); audio-only egress recording |
| FR-LIV-011 | F-2 §4.3 RPC `bluelab.end_call` (confirmed) + §5 buyer natural close — both → completed (AC-LIV-005) |
| FR-LIV-012 | F-2 §5: agent 15-minute maximum — conclude near limit, auto-complete at it; `max_call_seconds` in dispatch payload |
| FR-LIV-013 | T-2 transactional grading enqueue (F-3 §2 `grade_attempt`); SPA routes rep → attempt/review poll, candidate → plan; test calls discard |
| FR-LIV-014 | F-2 §5: 30 s reconnection grace then interrupted (T-6), never graded |
| FR-LIV-015 | T-6 reverses allowance for rep/author (AC-LIV-006); candidate → restart policy (FR-CND-010) |
| FR-LIV-016 | Never-established: join deadline + `DELETE /calls/current` + `503 call-capacity` — nothing recorded or consumed, retry offered |

## FR-SCR — Scoring & Review

| FR | Realized by |
|---|---|
| FR-SCR-001 | Call-completion job `grade_attempt` (F-3 §2), enqueued in T-2 — test/interrupted excluded by disposition |
| FR-SCR-002 | Grading worker inputs contract (F-3 §2): transcript + demeanor labels + frozen rubric + answer key; recording never an input (ADR-0009) |
| FR-SCR-003 | Grade-once identity `scorecard.attempt_id` (T-3); immutable review payload (AC-SCR-002) |
| FR-SCR-004 | `ReviewView.rubric_breakdown` (per-dimension score+note) + `overall` (weight-weighted, server-computed) |
| FR-SCR-005 | `ScoreBand` pairing on every rendered score, server-banded — clients never re-derive |
| FR-SCR-006 | `ReviewView.takeaway` |
| FR-SCR-007 | `ReviewView.moments[]` — timestamped, severity-tagged, dimension-linked; amber/red carry quote/try-instead/why |
| FR-SCR-008 | Commentary fields English; `quote`/`text` verbatim call-language (schema descriptions mark RTL) |
| FR-SCR-009 | Retry + `grading_pending` status (participant reads *preparing* — never an error, AC-SCR-005); `ops_fault` → `GET /ops/v1/faults` + `resolve` |
| FR-SCR-010 | `GET /attempts/{id}/review` — full composition in one response |
| FR-SCR-011 | `ReviewView.playback` (`open_at_ms`, `pinned_moment_id` = first red, else amber, else start; per-moment seek via `at_ms`) |
| FR-SCR-012 | Client-side filter/sort over the bounded `moments[]` (severities carried; empty states derivable) |
| FR-SCR-013 | `playback.recording_status: unavailable` with review complete; fault surfaced (`playback_asset`) |
| FR-SCR-014 | `AttemptView` — drill, participant-implicit, times, duration, status set, restart flag |
| FR-SCR-015 | `GET /drills/{id}/my-history` stats (best/latest/average/trend, list newest first — AC-SCR-006) |
| FR-SCR-016 | `GET /team/drills/{id}/stats` rollup (attempted-of-eligible, totals, average, per-rep best) |
| FR-SCR-017 | Concealment as projection: `weight` present only for author/team-manager (`AC-SCR-003`); concealed set only in `DrillFull` (author/manager-only endpoint); commentary generated concealment-safe (F-3 §4); PDF minus concealed elements |
| FR-SCR-018 | Review access rule on `GET /attempts/{id}/review`: rep own; manager team + their positions' candidates (replay `context`); self-authored private to author (404 to manager, AC-TRP-004); candidates always 404 |

## FR-TRP — Training: Rep

| FR | Realized by |
|---|---|
| FR-TRP-001 | `GET /me/progress` (monthly rating+delta+direction, weekly trend w/ call-type filter, weakest-first 30-day cards) |
| FR-TRP-002 | Same canonical derivation serves `/me/progress` and `/team/reps/{id}` (data 02 §3 views; AC-TRP-002) — a property, not a parallel computation |
| FR-TRP-003 | `GET /me/coach-feedback` + `POST /me/coach-feedback/mark-read` (unread count; rep-only) |
| FR-TRP-004 | `GET /me/profile` (lifetime figures) |
| FR-TRP-005 | `ProfileView.badges` (earned w/ rule, locked w/ goal; owner-only surface) |
| FR-TRP-006 | `GET /me/library` (`LibraryCard`: call type, source, label, best-or-unattempted, assigned due/allowance; brief + history links) |
| FR-TRP-007 | `source` tab param + live `counts` (AC-TRP-003) |
| FR-TRP-008 | `call_type`/`attempted`/`sort` params |
| FR-TRP-009 | `POST /drills` as rep → `self_authored` private drill; invisible outside the author everywhere (404; AC-TRP-004) |
| FR-TRP-010 | Counted-pool exclusion of self-authored attempts is the derivation's rule (V-1) — visible in every rating surface; history still shows them (V-8) |
| FR-TRP-011 | `GET /drills/{id}/my-history` (primitives + list + practice-again → brief) |
| FR-TRP-012 | Chart points derive from the same history payload (documented in `DrillHistory`) |
| FR-TRP-013 | `LibraryCard.assignment.locked` + used/allowed; admission `409 allowance-exhausted` (AC-TRP-005) |

## FR-TRM — Training: Manager

| FR | Realized by |
|---|---|
| FR-TRM-001 | Team scope ambient on every `/team/*` operation ([00 §4](00-contract-overview.api.md); AC-TRM-006) |
| FR-TRM-002 | Server-side canonical views (V-1..V-5) feed dashboard/roster/deep-dive — formulas never client-side |
| FR-TRM-003 | `TeamDashboard.tiering_suppressed` + tier counts; `RosterRow.tier` (null while suppressed) |
| FR-TRM-004 | `GET /team/dashboard` (average+delta, tier counts, gap rows sorted, severity marks — AC-TRM-002) |
| FR-TRM-005 | `GET /team/roster` (sorted by rating; tier pill; strongest/weakest; row → deep dive) |
| FR-TRM-006 | `GET /team/reps/{account_id}` (rating+trend, per-call-type, recent counted attempts → replay) |
| FR-TRM-007 | `GET /attempts/{id}/review` as manager: `context.viewer: replay` + participant name + attempt number, read-only, weights visible (AC-TRM-003) |
| FR-TRM-008 | `GET /team/drills` (published + drafts, status/average/count/updated) |
| FR-TRM-009 | `GET /team/drills/{id}/stats` (rollup + leaderboard w/ replay ids — AC-TRM-004) |
| FR-TRM-010 | `GET /drills/{id}` full basis (read-only frozen content) + test call (`mode: test`) + archive |
| FR-TRM-011 | `PUT /drills/{id}/assignment` (recipients, due date, allowance; appears in recipients' libraries; **no email** — no E-kind exists for assignment) |
| FR-TRM-012 | Same PUT: single-assignment upsert, fresh allowance semantics (AC-TRM-005) |
| FR-TRM-013 | Same PUT any time; frozen content untouched by construction (assignment is not drill content) |
| FR-TRM-014 | `GET /team/cohorts` (four computed cohorts) |

## FR-HIR — Hiring: Manager

| FR | Realized by |
|---|---|
| FR-HIR-001 | `GET /positions` (status + three counts; adaptive action derives client-side from counts) |
| FR-HIR-002 | Server-computed `totals` on `GET /positions` — open positions, pending review, approved-unsent — independent of pagination (gate F-4) |
| FR-HIR-003 | `POST /positions` (title, openings, E-5 toggle, report policy; language displayed fixed) |
| FR-HIR-004 | `PUT /positions/{id}/assessment` (ordered drill_ids from team catalog; inline authoring via standard `POST /drills` then referencing) |
| FR-HIR-005 | Activation on first save (≥ 1 stage); freeze at first invite → `409 assessment-frozen` (AC-HIR-001) |
| FR-HIR-006 | `POST /positions/{id}/candidates` (batch entries + internal_note); `PATCH /candidates/{id}` (note edit); note never in any candidate-reachable schema |
| FR-HIR-007 | `POST /positions/{id}/candidates/parse` (template file → reviewed rows; never invites directly); the downloadable template is an SPA-bundled static asset (gate F-9) |
| FR-HIR-008 | `POST /positions/{id}/invites` (expiry preset/default 7, template edit, T-7 tokens + E-2; one attempt per drill enforced at admission) |
| FR-HIR-009 | `POST /candidates/{id}/invite-resend` (fresh token per token rules; confirmation client-side) |
| FR-HIR-010 | `GET /positions/{id}/candidates?view=` (three tabs, live counts, per-state actions from row fields; invite delivery status from E-2 events) |
| FR-HIR-011 | `GET /candidates/{id}/report` (identity, drills+time, unweighted-mean overall banded, internal note distinct, cross-drill takeaway, per-drill cards → reviews with weights — AC-HIR-003) |
| FR-HIR-012 | `GET /candidates/{id}/report/pdf` (+ `POST .../render` retry) — concealment-safe single artifact (AC-HIR-004); embedded preview is SPA rendering of the same URL |
| FR-HIR-013 | `PUT /candidates/{id}/decision` (approve/reject; pending by omission; `409 decision-frozen` after shortlist — AC-HIR-005; approve-and-next is SPA routing over review_queue) |
| FR-HIR-014 | `GET/POST/DELETE /hr-contacts` + `POST /positions/{id}/shortlists` (T-9: recipients saved+free-entry, editable body, PDFs attached, holdback by exclusion — AC-HIR-006) |
| FR-HIR-015 | `POST /positions/{id}/close` (T-8: tokens expired, entry stopped, read-only archive — AC-HIR-007) |
| FR-HIR-016 | `PipelineCandidate.expired_incomplete` + `CandidateReport.incomplete` (completed drills graded and reported; decidable) |
| FR-HIR-017 | E-5 on completion when toggled (F-3 §3) |
| FR-HIR-018 | `POST /ops/v1/positions/{id}/transfer` (subtree cascade; HR lists stay behind) |

## FR-CND — Candidate Flow

| FR | Realized by |
|---|---|
| FR-CND-001 | `GET /assessment` (personalized welcome — name, position, org; how-it-works/preparation guidance is SPA copy over `state: preflight_required`) |
| FR-CND-002 | `POST /assessment/preflight` checks (mic/audio/connection, independent pass-fail, 422 guidance handles + retry; AC-CND-001) |
| FR-CND-003 | Same POST: consent + terms as distinct instruments; language displayed from `AssessmentView.language` |
| FR-CND-004 | `AssessmentView.stages[]` statuses (completed/next/upcoming), strict order at brief + admission (`409 stage-not-next`; AC-CND-002) |
| FR-CND-005 | `GET /assessment/stages/{id}/brief` + `/reference` (standard brief in candidate chrome) |
| FR-CND-006 | `CallGrant.stage {ord, of}` + F-2 controls limited to mute + end-call |
| FR-CND-007 | Completion returns to plan with next unlocked; **no evaluative field exists in any candidate schema** (AC-CND-003 — checkable in the spec) |
| FR-CND-008 | `AssessmentView.completion` terminal state; token admits only it (FR-IDA-013) |
| FR-CND-009 | `GET /assessment` resume semantics — any device, single-active-call rule at admission (AC-CND-005) |
| FR-CND-010 | Interruption → stage `next` with `restart_available`; restart carries flag; second interruption → `closed_incomplete` + `409 stage-consumed` (AC-CND-004) |
| FR-CND-011 | Expiry mid-assessment: `401 token-expired`; completed stages remain graded; resend resumes same state |
| FR-CND-012 | E-3 per position policy (F-3 §3: on completion / on reject / never — AC-CND-006) |

## NFR / CMP contract touchpoints

| ID | What the contract carries |
|---|---|
| NFR-001 | F-2 §8: per-turn latency emitted at the session runtime (Phase 11 input); no HTTP call sits inside a turn |
| NFR-002 | `503 call-capacity` admission refusal protects quality of running calls; lease/registry sizing (F-2) |
| NFR-003 | Journey surfaces are plain HTTP + LiveKit — synthetically checkable end-to-end (Phase 11 owns the checks) |
| NFR-004 | Frozen-basis grading inputs (F-3 §2) + `grading_meta` provenance (data schema); contract keeps grading server-side only |
| NFR-005 | Poll pattern on `GET /attempts/{id}` sized to the 20/45 s budget; transcript complete at T-2 so grading needs no further interface |
| NFR-006 | No unmetered model surface: generation/relevance/render throttled ([05 §4](05-rate-limits-and-quotas.api.md)); grading queue-paced |
| NFR-007 | Pre-flight `checks.connection`/`microphone` gate (FR-CND-002); F-2 uses standard WebRTC via LiveKit across the four desktop browsers |
| CMP-001 | `/ops/v1/erasure-requests` + `/ops/v1/export-requests` for reps, managers, candidates; erasure reaches object store (F-3 §5) |
| CMP-002 | Consent capture: `POST /auth/first-sign-in`, `POST /auth/acceptances`, `POST /assessment/preflight`; universal backstop `409 consent-required` at admission; events recorded with notice version |
| CMP-003 | `422 not-job-relevant` generation gate; identical-assessment freeze (`assessment-frozen`); every scorecard element dimension- and transcript-anchored in `ReviewView` |
| CMP-004 | Org ambient in every request; cross-org = `404 not-found` indistinguishable (AC-IDA-006); candidate tokens bound to org+position |
| CMP-005 | Terms acceptance at both gates as a **distinct** instrument from consent (separate fields, separate records, never merged) |

## Coverage statement

All 131 functional requirements across the nine families have at least one realizing contract element
above; the gate's walkthrough exercises each row from the consumer side against the mock server
([ADR-0035](adr/0035-contract-first-openapi.md); see [README §Gate](README.md)). Rows marked SPA name
client obligations the Phase 6/13 work inherits; rows citing F-2/F-3 are testable against the contract
documents' schemas rather than the mock.
