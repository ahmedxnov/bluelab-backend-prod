# BlueLab v1 — Security Requirements

**Responsibility:** every testable security requirement (`SEC-*`) of v1 — the requirements produced by the
security and compliance design ([security/](../security/README.md)) and fed back into the specification so that
each is verifiable and each can fail. Other files cite these IDs. This file owns no feature behavior and states
no NFR or compliance target already owned by [01](01-nfr-and-compliance.spec.md); it adds the security
constraints those and the functional requirements imply.

**Added as Amendment A-3 (see [00 §Amendments](00-overview.spec.md)).** These requirements do not change any
functional requirement, the scope line, or any tiered figure; they make the system's security posture explicit
and testable. The design rationale for each lives in the referenced security document; the requirement here is
the contract verification checks against.

---

## Conventions

- Each requirement carries a priority (`[Must]` — v1 does not ship without it · `[Should]` — important, slips
  last) and a **Verified by** line naming the check that can fail it.
- Requirements are grouped by theme; the ID sequence is stable and gapless.
- `[op]`-tunable operational values (throttle numbers, session lifetimes) are owned by
  [security/](../security/README.md) and [01 §3](01-nfr-and-compliance.spec.md) where tiered; this file fixes the
  *behavior*, not the number, except where a number is itself the requirement.
- These requirements are **invariant across rollout tiers** unless a tier row in [01 §3](01-nfr-and-compliance.spec.md)
  says otherwise; the tier-specific security posture is [security/03](../security/03-tiers-and-compensating-controls.security.md).

---

## 1. Authentication & session (security/04)

### SEC-001: Session lifetime and rotation `[Must]`
Account-holder and operator sessions shall be server-side and opaque, with a **12-hour idle** and **7-day
absolute** expiry, and the session identifier shall be **rotated on every privilege change** (first-sign-in
completion, acceptance-gate clearance). Deactivation or sign-out shall revoke the session immediately.
**Verified by:** an expired/idle session is refused; a rotated identifier replaces the prior one on privilege
change; a deactivated account's live session stops working within one request.

### SEC-002: Host-locked session cookies `[Must]`
Every session cookie (customer and operator) shall carry the `__Host-` prefix with `HttpOnly`, `Secure`,
`SameSite=Strict`, `Path=/`, and no `Domain`.
**Verified by:** cookie-attribute inspection on both surfaces; a startup audit fails the build if a session
cookie lacks the prefix or any attribute.

### SEC-003: Candidate token strength and binding `[Must]`
Candidate tokens shall be ≥ 256-bit values from a cryptographic source, stored **hashed**, transported only via
the `Authorization` bearer header and the invite-link URL **fragment**, and validated against their stored
`(org, position, candidate)` binding on every request — never for existence alone.
**Verified by:** token entropy and hashed-at-rest check; a token used outside its binding is refused; the token
never appears in a path, query string, or server log.

### SEC-004: Auth backoff, never hard lockout `[Must]`
The sign-in surface shall throttle by IP and by email and apply **exponential backoff** on repeated failure,
and shall **never** apply a hard account lockout keyed on an email. A failed sign-in shall not disclose whether
the account exists.
**Verified by:** repeated failures back off without ever locking an account out; the response is identical for
an existing and a non-existent account.

### SEC-005: Failed-authentication alerting `[Must]`
Failed sign-ins, tripped throttles, and invalid-token floods shall be logged as security signals with the same
principal/IP identities used elsewhere, sufficient for alerting.
**Verified by:** a simulated credential-stuffing run produces a queryable, alertable signal.

### SEC-006: Origin-check coverage `[Must]`
Every state-changing request on a cookie-authenticated surface shall be rejected when its `Origin` /
`Sec-Fetch-Site` indicates a cross-site initiator, in addition to `SameSite=Strict`.
**Verified by:** a cross-site unsafe-method request is rejected on every mutating route; route coverage is
checked, not sampled.

### SEC-007: No-login-entry abuse watch `[Should]`
Automated abuse of the candidate pre-flight and token surfaces (which carry no CAPTCHA by accessibility design)
shall be observable and rate-bounded.
**Verified by:** pre-flight and invalid-token request rates are throttled and surfaced; an automated probing run
is bounded and visible.

## 2. Authorization & isolation (security/02, security/04)

### SEC-008: Tenant and team isolation `[Must]`
No request in any role shall read or affect another org's or team's data. Isolation shall be enforced at the
data layer by forced row-level security and mandatory scope filtering, with the application's scoped-session
factory as a second belt; all data access shall go through the application, never a database auto-API. The
application's database role shall be a **non-superuser role without the RLS-bypass attribute**, so
`FORCE ROW LEVEL SECURITY` binds it and the policies cannot be silently bypassed by the connecting principal.
The scope context shall be set **transaction-locally** (`SET LOCAL` / `set_config(..., is_local => true)`), never
at session scope, so a connection reused by a transaction-mode pooler cannot inherit a prior transaction's scope
and leak across tenants.
**Verified by:** the isolation suite attempts cross-org and cross-team access on every data category and role —
through the derived views, not only base tables — all denied; run before the first real person's first call at
every tier; a role-privilege check confirms the application role can neither bypass nor disable RLS; and a
**pooled-connection reuse test** confirms a second tenant's transaction on a reused backend connection inherits
no scope GUC from the first.
*(Realizes [CMP-004](01-nfr-and-compliance.spec.md) with the structural mechanism.)*

### SEC-009: Deny-by-default and unforgeable references `[Must]`
Absence of a grant shall be a denial; object references shall be non-enumerable; authorization shall evaluate
`(scope, role, authorship, product context)`, never role alone.
**Verified by:** an unscoped query cannot be expressed against the store; a rep session cannot mount a manager
or ops route; a non-author cannot obtain a concealed element by any reference.

### SEC-010: Denial indistinguishable from non-existence `[Must]`
A cross-scope reference shall answer `404` identically in status, body, **and timing** to a truly absent id; no
`forbidden` response shall exist for customer data.
**Verified by:** response-body equality and a timing-distribution comparison between absent and cross-scope ids
under load show no distinguishable difference.

## 3. Email egress & output handling (security/02, security/05)

### SEC-011: Shortlist recipient confirmation `[Must]`
Before a shortlist of candidate PII is sent to free-entry HR addresses, the system shall present the resolved
recipient list for explicit confirmation and warn on any address whose domain has not previously received a
shortlist from this manager.
**Verified by:** an E-4 send cannot proceed without recipient confirmation; a new-domain recipient raises a
warning; recipients are snapshotted as sent.

### SEC-012: No report dispatch before concealment-safe render `[Must]`
A candidate report shall not be dispatched (to HR or to the candidate) before its concealment-safe PDF is
confirmed rendered; the send-with-pending-PDF path shall be verified before any relaxation of this gate.
**Verified by:** a dispatch attempt with a pending/failed PDF is refused; the dispatcher's pending-PDF semantics
are tested (realizes [C-2](../ux/README.md)).

### SEC-039: Presigned-URL discipline for stored media `[Must]`
Access to DC-4 recordings and DC-3 report PDFs shall be by **short-lived, single-object presigned URLs issued
only after authorization**, never by a durable or broadly-scoped link; the URL shall not be written to a server
or access log, and its lifetime shall be the minimum the playback/download flow needs. A presigned URL is a
bearer capability to the most sensitive asset and is treated as one — the [05 §2](../security/05-data-protection-and-secrets.security.md)
"DC-4 never travels in a URL" rule is honored by keeping the *content* out of the URL while bounding the *access
grant* the URL represents.
**Verified by:** an issued URL scopes to exactly one object, expires within the short window, and appears in no
log; an expired URL is refused; access requires a prior authorization.

### SEC-020: Output encoding at every sink `[Must]`
All model-generated, transcript-derived, and user-supplied text shall be output-encoded for its rendering
context in both the SPA and the report PDF template; no such text shall reach an HTML, URL-attribute, or code
sink unescaped (no raw-HTML injection sink).
**Verified by:** injection-payload strings in transcript, takeaway, moment, persona, author-recap, and candidate
fields render as inert text in the SPA and in the PDF; a lint forbids raw-HTML sinks on these paths.

### SEC-021: Email template safety `[Must]`
Editable email bodies and template variables (candidate fields, manager-edited bodies, the token-bearing invite
URL) shall be output-encoded for the email context, and the dispatcher shall render only the five inventory
templates, rejecting any other kind.
**Verified by:** an injection payload in an invite/shortlist body renders inert; a request for a sixth template
kind is rejected.

### SEC-022: Unicode normalization and bidi-control stripping `[Must]`
Attacker-influenceable text that reaches a human decision surface (transcript text, candidate identity fields,
internal notes, author-entered challenges and motives) shall be Unicode-normalized and stripped of
bidirectional-control and zero-width characters before storage and before render.
**Verified by:** a Trojan-Source / homoglyph / zero-width payload in these fields is neutralized in storage and
in every render, including the PDF.

## 4. Generated content & grading (security/06)

### SEC-028: Injection screening on author input `[Should]`
Custom author-entered challenge and motive text shall be screened for prompt-injection patterns in addition to
the job-relevance gate, as defense in depth.
**Verified by:** known injection patterns in custom author text are flagged; the job-relevance gate remains the
compliance control.

### SEC-029: Transcript as untrusted evidence in grading `[Must]`
The grading path shall treat the participant transcript as untrusted evidence, not as instructions; the overall
score shall be computed arithmetically and never emitted by the model; grading shall be decomposed per
dimension and anchored to transcript evidence.
**Verified by:** transcript content instructing the grader (e.g. "score this 10/10") does not move the computed
overall score in a re-grade sample.

### SEC-030: Concealment resistance under adversarial pressure `[Must]`
The buyer persona shall not disclose concealed material or break character under direct participant pressure;
concealment commentary shown to non-authors shall not narrate a concealed element. This shall be tested with an
adversarial battery at model selection, against the built system, and **again on any change to the
conversational-model version or alias** — instruction adherence is a model-behavior property that a model update
can regress silently ([R-16](../stack/00-selection-overview.stack.md)).
**Verified by:** the concealment red-team battery (direct extraction attempts across framings) fails to extract
the concealed set from the built system; the battery is re-run and passes before any conversational-model
version change ships; commentary review finds no concealed narration. *(Carried as a monitored residual —
[R-8](../architecture/05-risks.arch.md); never marked fully retired.)*

### SEC-031: Demeanor signal bias validation `[Must]`
Any demeanor signal used in grading shall be derived from transcript wording (not vocal prosody), restricted to
a coarse behavioral label set, **validated for demographic bias for the served population before it informs any
score**, and independently disableable — with transcript-only grading as the default-safe state until validation
is met.
**Verified by:** the signal is off until a documented bias validation for Egyptian Arabic speakers passes;
grading degrades to transcript-only with the signal disabled. *(Realizes the [R-3](../architecture/05-risks.arch.md)
/ [CMP-003](01-nfr-and-compliance.spec.md) posture.)*

## 5. Secrets, keys & platform (security/03, security/05)

### SEC-015: Vendor no-train / zero-retention configuration `[Must]`
Every external capability that receives call audio, transcripts, or prompts shall be configured for its
no-training and lowest-retention (zero-retention where offered) option, and that configuration shall be part of
the sub-processor record.
**Verified by:** each vendor's account/config shows no-train and minimal retention; the sub-processor inventory
records it.

### SEC-016: PDF render sandbox isolation `[Must]`
The report-rendering engine shall run network-isolated with external, `file:`, and instance-metadata fetches
blocked; its inputs shall be the participant-safe projection only (never the concealed set, weights, or internal
note).
**Verified by:** a render given content referencing an external/`file:`/metadata URL performs no such fetch; the
render principal cannot read the concealed tables.

### SEC-017: Upload and import safety `[Must]`
Knowledge-file uploads shall be type- and size-limited (≤ 20 MB) and refused at selection otherwise; extraction
shall run isolated and reading ids only; bulk-candidate import cells shall be treated as data, never as
formulae (formula/CSV-injection neutralized).
**Verified by:** an oversize/unsupported upload is refused; a formula-prefixed import cell is stored as literal
text; extraction failure leaves live facts untouched.

### SEC-018: Erasure completeness and restore honesty `[Must]`
Erasure shall remove the person's identifying, contact, and content data across the database and object store,
leave only the statistical residue, verify zero-remain, and record per-category evidence; every restore shall
replay executed erasures so no restore resurrects an erased subject; erasure shall propagate to backup copies
within 24 hours. Erasure shall **quiesce the subject's in-flight and queued work** — cancel or await any pending
grading, report-render, or synthesis job for the subject before zero-remain verification, and a worker shall
**abort if its subject has been erased** — so no job completing after erasure re-writes the person's data (an
idempotent insert offers no protection here: the rows were deleted, so there is no conflict to suppress).
**Verified by:** an executed erasure leaves no transcript/moment/PII/object; a restore-then-reconcile of a point
before an erasure re-applies it; the off-copy reflects the deletion within a day; **a job enqueued before an
erasure and run after it writes nothing about the subject** (routed to the erasure procedure —
[security/08 RB-11](../security/08-security-requirements-and-routebacks.security.md)).
*(Realizes [CMP-001](01-nfr-and-compliance.spec.md) erasure; the posture is [OQ-1](../data/README.md).)*

### SEC-019: Secret scoping and blast-radius bound `[Must]`
Secrets shall be stored in a managed secret store (or SecureString parameters at the demo tier), never in code,
images, logs, or a hand-edited file; access shall be per-plane least privilege where the composition allows;
every vendor key shall carry a hard spend cap with an 80 % warning that bounds a stolen key's spend.
**Verified by:** no secret value in any repository, image, or log; a plane role cannot read another plane's
keys (fleet); each vendor key has an enforced cap and warning.

### SEC-023: Supply-chain and model-version integrity `[Must]`
Dependencies and container images shall be version-pinned and integrity-verified; model dated snapshots shall be
pinned where offered, with the grading-variance sample re-run on any model alias change; no public/partner API
or long-lived API key shall exist.
**Verified by:** a lockfile/image-digest check; a model alias change triggers a variance re-run; no external API
credential exists.

### SEC-024: Content-free telemetry `[Must]`
Observability (metrics, traces, logs) shall carry identifiers and timings only; no DC-3/DC-4 content shall
appear in any log, span, or metric label.
**Verified by:** a telemetry scan finds no personal content; a review of log statements on the call and grading
paths confirms content-free emission.

### SEC-025: Demo-host metadata hardening `[Must]`
On the single-host demo composition, instance metadata shall require the session-oriented service (IMDSv2) with
a metadata hop limit of 1, and **egress to the metadata address (`169.254.169.254`) shall be denied host-wide —
from every container, not only the render container** — since any co-resident process sharing the single instance
role is an equally dangerous pivot. The hop-limit-1 control protects only containers on a bridge network (which
adds the extra hop); containers must therefore not run with host networking, so the host-wide egress deny is the
control that holds regardless.
**Verified by:** a request to instance metadata from any container is refused; IMDSv1 is disabled; no container
runs with host networking.
*(Realizes [C-5](../infra/README.md) / [ADR-0055](../security/adr/0055-single-host-blast-radius-controls.md).)*

### SEC-026: No tier-conditional security code `[Must]`
No application-code branch shall key on the rollout tier; the tier shall live only in infrastructure composition
and configuration values. A security control shall behave identically at every tier.
**Verified by:** a code scan finds no tier-conditional branch; the same test suite passes against every tier's
build. *(Realizes [C-6](../infra/README.md).)*

### SEC-027: Encryption at rest and key-management lever `[Must]`
All data at rest — transactional store, object store, backups, secrets — shall be KMS-encrypted, and all data in
transit shall be TLS ≥ 1.2; a customer-managed key shall be adoptable without redesign when key-rotation control
or access logging on the key is required.
**Verified by:** every store reports encryption at rest; no plaintext hop; the CMK path is exercised in a
non-prod environment.

### SEC-036: Uniform capability-unavailable behavior on the call path `[Must]`
A capacity- or spend-cap-induced unavailability on the call path shall surface only through the existing
**failure-to-establish** path at admission (nothing recorded, nothing consumed — [FR-LIV-016](13-live-call.spec.md))
or the **interruption** path mid-call (void, allowance untouched — [FR-LIV-014](13-live-call.spec.md)), and shall
be **indistinguishable** to the participant from a genuine vendor outage — no distinct "budget exhausted" signal,
no queue position, no cause text. The demo per-vendor spend caps ([01 §3.5](01-nfr-and-compliance.spec.md)) are
the one place a *budget*, not a fault, triggers this, and they reuse the fault path rather than inventing a new
one.
**Verified by:** a call blocked by a bitten spend cap records nothing and consumes nothing and produces the same
participant-visible state as a simulated vendor outage; no response field distinguishes the two.
*(Reconciles the [01 §3.5](01-nfr-and-compliance.spec.md) / [infra 01 §7.2](../infra/01-topology-and-networking.infra.md)
spend cap with [FR-LIV-016](13-live-call.spec.md) and the UX indistinguishability; the contract mapping is routed
to the API set as [security/08 RB-9](../security/08-security-requirements-and-routebacks.security.md).)*

### SEC-037: Coordination-store protection `[Must]`
The coordination store holds DC-5 material — server-side session records keyed by the value carried in the
session cookie, and the single-live-call leases. It shall be **encrypted at rest, reachable only over TLS,
access-token-authenticated, and network-isolated** to the plane security groups; on the single-host demo it is a
container on the box, inside the accepted host blast radius ([security/03](../security/03-tiers-and-compensating-controls.security.md)),
never exposed off-host.
**Verified by:** the store rejects unauthenticated and non-TLS connections and is unreachable from outside the
plane security groups; a read of the store's session keys cannot be replayed as a valid session because session
material is not otherwise disclosed; at the demo tier the store binds only to the host.

### SEC-038: Inbound provider-callback hardening `[Must]`
The internet-reachable provider callbacks (the media webhook and the email delivery-event ingestion) shall
**verify the provider signature before performing any work**, be **rate-limited at the edge**, drop malformed
floods, and process **idempotently by provider message id** — and they shall be treated strictly as
reconciliation input, never as a primary state driver.
**Verified by:** an unsigned or bad-signature callback is refused before any state change; a replayed valid
callback is a no-op; a callback flood is bounded and does not exhaust the host; forged delivery events cannot
alter an already-recorded delivery state.

### SEC-041: Launch data-layer network restriction `[Must]`
While the transactional store is reached over a public endpoint (the cross-provider launch seam), access shall be
**restricted to the platform's source addresses** (allowlist / private path where the provider offers it), the
database-role authentication shall be **brute-force-throttled**, and the application's database role on that
provider shall be **verified non-superuser without the RLS-bypass attribute** ([SEC-008](02-security-requirements.spec.md)).
The public seam closes at the in-VPC migration ([stack 00 §9](../stack/00-selection-overview.stack.md) R-17).
**Verified by:** the database endpoint refuses connections from outside the allowlist; repeated failed DB-auth
attempts are throttled; the provider's app role reports neither superuser nor `BYPASSRLS`.

## 6. Operations & compliance (security/04, security/07)

### SEC-013: Operations-surface network restriction `[Must]`
The operations surface shall be reachable only from the operator address ranges and shall never be served behind
the public content edge.
**Verified by:** an ops request from outside the allowed ranges receives the same generic 404 as any unknown
path; the surface is unreachable from the customer edge.

### SEC-014: Operator and break-glass second factor `[Must]`
Operator sign-in and any break-glass infrastructure access shall require a second authentication factor; every
break-glass use shall be audited and shall page the team. Break-glass reuses the cloud provider's IAM MFA; the
operations console needs a second-factor (TOTP) primitive the identity-primitives design did not provision —
routed as [security/08 RB-7](../security/08-security-requirements-and-routebacks.security.md).
**Verified by:** ops and break-glass access without a second factor is refused; a break-glass use produces an
audit record and a page.

### SEC-040: Provisioning cannot become cross-tenant escalation `[Must]`
Because operations provisions accounts by supplying `{email, role, org}` freely and the initial credential is
mailed to that address ([FR-IDA-001](10-identity-and-access.spec.md)/[FR-IDA-003](10-identity-and-access.spec.md)),
a single operator could mint a customer principal at an address they control and read that org's DC-3 through the
customer surface — a legitimate-verb abuse the compromised-account controls do not cover. Provisioning shall
therefore be constrained: the target email domain SHOULD match the org's registered domain, **or** provisioning
into an org SHOULD require a second operator's approval — with the append-only ops audit as the backstop, not the
sole control.
**Verified by:** provisioning a mismatched-domain account is blocked or held for second-operator approval; every
provisioning action is audited with actor, target org, and reason; a review confirms provisioning alone cannot
silently create a readable principal in an arbitrary org.
*(The choice of domain-match vs second-operator approval is routed to the identity/operations flow —
[security/08 RB-10](../security/08-security-requirements-and-routebacks.security.md).)*

### SEC-032: Consent and terms capture `[Must]`
Recording consent and terms/privacy acceptance shall be captured as two distinct, never-pre-selected controls at
the account entry gate and at candidate pre-flight; no live call shall start for anyone without a consent record
for the current notice version; both records shall be retained and retrievable.
**Verified by:** neither gate completes without both instruments; a call attempted without a consent record is
blocked at admission; records are retrievable in test.
*(Realizes [CMP-002](01-nfr-and-compliance.spec.md)/[CMP-005](01-nfr-and-compliance.spec.md) as security checks.)*

### SEC-033: Cross-border transfer filing `[Must]`
Where personal data is processed outside Egypt, a PDPC cross-border transfer license covering the audio,
transcript, model, voice, email, and data-layer sub-processors shall be filed before the enforcement date, and
explicit recording consent shall stand as the standing lawful-basis backstop.
**Verified by:** the filing is submitted and tracked to completion before enforcement; the sub-processor
inventory it enumerates is current.
*(A dated dependency — [security/07 §2](../security/07-compliance-and-data-protection.security.md); realizes the
[R-9](../architecture/05-risks.arch.md) resolution on the default posture.)*

### SEC-034: Sub-processor inventory maintenance `[Must]`
The personal-data sub-processor inventory (processor, data, locality, term) shall be maintained current; adding a
processor shall be a filing amendment and an inventory update, not a configuration change.
**Verified by:** the inventory matches the deployed processors; a new processor cannot be introduced without an
inventory and filing update.

### SEC-035: Breach-assessment inputs `[Should]`
The data classification and the sub-processor inventory shall be maintained sufficient to answer, on a suspected
breach, what data was exposed, whose, and where it was processed, feeding the regulator/subject notification path
counsel defines.
**Verified by:** a tabletop breach exercise resolves scope, subjects, and processing locality from the
maintained inventory and classification.

---

## 7. Referencing rule

Feature and design files cite these `SEC-*` IDs where a behavior depends on them. The statements here are the
single source of truth for the security requirement; any restatement elsewhere is a defect. The design rationale
is owned by [security/](../security/README.md) and cited by ID, not restated.
