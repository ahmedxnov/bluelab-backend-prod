# BlueLab v1 — Non-Functional Requirements & Compliance

**Responsibility:** every measurable how-well target (`NFR-*`) and every compliance requirement (`CMP-*`) of v1 — stated here once, with how each is verified. Other files cite these IDs and never restate the numbers. This file owns no feature behavior. Since **Amendment A (2026-07-24)** it also owns the **v1 rollout tiers** (§3) — the only place where a tiered target is allowed to differ from the committed one.

---

## 1. Non-functional requirements

### NFR-001: Voice round-trip latency `[Must]`
During a live call, the elapsed time from the participant finishing an utterance to the AI buyer beginning its spoken response (one **turn handoff** — the unit of measurement) shall be **≤ 1.0 second at p50 and ≤ 1.5 seconds at p95**, both conditions holding simultaneously, measured over all turn handoffs across all production calls in any 24-hour window.
**Verified by:** per-turn latency instrumentation on live calls; load test at target concurrency asserting both percentiles.

### NFR-002: Concurrent live calls `[Must]`
The system shall sustain **50 concurrent live calls** at launch quality (NFR-001 holding), with a validated growth path to **500 concurrent calls** demonstrated under load test before any capacity commitment beyond 50.
**Verified by:** load test at 50 with NFR-001 assertions; scale test to 500.
**Tiered (§3):** the figures above are the **Tier 3** commitment. Tiers 1 and 2 ship a lower concurrency ceiling, stated once in §3; the ceiling of the tier in force is enforced at admission whatever its value.

### NFR-003: Availability `[Must]`
The platform shall achieve **≥ 99.5% monthly availability** on its two critical journeys — a participant conducting a live call end-to-end, and a candidate completing an assessment — measured by synthetic checks of those journeys.
**Verified by:** journey-level synthetic monitoring; monthly availability reporting.
**Tiered (§3):** the figure above is the **Tier 3** commitment. Tiers 1 and 2 run a lower, best-effort target on a redundancy posture stated once in §3. The *journeys* being measured, and the synthetic checks that measure them, are identical at every tier — only the target and the redundancy behind it move.

### NFR-004: Grading consistency `[Must]`
Re-grading the same completed call against the same rubric and the same published facts shall yield an overall score within **± 0.5 (of 10)** and every dimension score within **± 1.0** of the original, in **≥ 95%** of a re-grading sample.
**Verified by:** automated re-grade sampling across call types; report of score deltas.

### NFR-005: Grading turnaround `[Must]`
A completed call's review shall be available within **20 seconds at p50 and 45 seconds at p95** of call end, both conditions holding simultaneously, under launch concurrency. (The call's transcript is produced live during the call and is complete at call end — [FR-LIV](13-live-call.spec.md) — so this budget is evaluation work only.)
**Verified by:** grading-pipeline timing instrumentation under load, asserting both percentiles.

### NFR-006: Unit cost `[Must]`
The variable cost of a completed drill attempt (voice + AI, all external-service calls included) shall be **≤ USD 1.00 at p95** across production attempts at launch configuration; the platform maximum call length ([FR-LIV-012](13-live-call.spec.md)) bounds the worst-case attempt cost.
**Verified by:** per-attempt cost accounting on production traffic, asserting the p95.

### NFR-007: Platform target `[Must]`
All user-facing experiences shall function fully on the current and previous major versions of Chrome, Edge, Firefox, and Safari on desktop, including microphone capture and audio playback.
**Verified by:** cross-browser test pass on the critical journeys.

## 2. Compliance requirements

### CMP-001: Egyptian personal-data protection `[Must]`
The system shall process personal data in accordance with Egypt's Personal Data Protection Law (PDPL), including: a lawful basis for processing each category of personal data it holds; the ability to export a person's data on request; and the ability to permanently erase a person's data on request. Erasure and export cover reps, managers, and candidates.
**Verified by:** data-inventory review mapping every personal-data category to its basis, export path, and erasure path; erasure/export executed and evidenced in test.
*Legal counsel confirms the detailed obligations; this requirement binds the product to support them.*

### CMP-002: Recording consent `[Must]`
Consent to audio recording, transcription, and AI evaluation shall be captured explicitly, **once per person per notice version**, at the person's entry into the product: for **account holders** (managers, reps), a consent checkbox presented during first sign-in as part of the forced password-change step — completing first sign-in requires selecting it; for **candidates**, the consent notice at pre-flight, before their first call. The checkbox is its own distinct, never pre-selected control with the notice text (or a link to it) beside it. As a universal backstop, **no live call of any kind shall start for a person without their consent on record**, whatever path they arrived by. Consent is re-requested only when the notice materially changes. The consent event (who, when, which notice version) shall be retained as a record.
**Verified by:** first sign-in cannot complete without consent (accounts); pre-flight cannot pass without consent (candidates); a call attempted without a consent record is blocked; no repeat prompts; consent records retrievable in test.

### CMP-003: Job-relevant assessment only `[Must]`
**All grading — of reps and candidates alike — shall evaluate job-relevant selling conduct on the call, and nothing else.** This includes what the participant said and did in the conversation and their **demeanor toward the buyer as a behavior** (e.g. aggressive, dismissive, warm, patient) where job-relevant. It excludes any assessment of protected or non-job-relevant personal characteristics (gender, age, religion, personality profiling, or the like) and of **voice, speech, and language identity traits** — accent, pitch, timbre, fluency, vocabulary sophistication, or formal-versus-colloquial register — which shall never be graded, as distinct from job-relevant conduct. Using correct product and industry terminology, and treating the buyer professionally rather than rudely, are job-relevant conduct and remain in scope; eloquence and speech register are not, in either product (in Egyptian Arabic especially, register maps onto class, schooling, and region — grading it would screen on background, not skill). Every scorecard element shall remain explainable — traceable to a rubric dimension and transcript evidence. **Any demeanor or emotion signal used in grading shall be validated for the population it serves (e.g. Egyptian Arabic speakers) against demographic bias before it informs scores.** This binds rubric **generation** as well as grading: generated and custom-authored rubric dimensions, challenges, and motives shall be job-relevant, whatever the author enters ([FR-DRL](12-drill-lifecycle.spec.md)). In hiring, additionally, every candidate for a position is assessed with identical drills and rubrics (the comparability rule, [FR-DRL](12-drill-lifecycle.spec.md) / [FR-HIR](22-hiring-manager.spec.md)), keeping decisions defensible.
**Verified by:** rubric-content review across generated rubrics in both products; audit that every scorecard element traces to dimension + transcript anchor; bias spot-checks on training and hiring grading samples; pre-launch validation of the demeanor/emotion signal for the served population.

### CMP-004: Tenant isolation `[Must]`
Every piece of customer data shall belong to exactly one org, and no request in any role shall be able to read or affect another org's data. Candidate tokens are bound to their org and position.
**Verified by:** isolation test suite attempting cross-org access on every data category and role, all denied.

### CMP-005: Terms & privacy-notice acceptance `[Must]`
At each entry path's gate — an account holder's first sign-in ([FR-IDA-004](10-identity-and-access.spec.md)) and a candidate's pre-flight ([FR-CND](23-candidate-flow.spec.md)) — the system shall present the Terms of Use and Privacy Notice (as links with an acceptance acknowledgment) and record the acceptance (who, when, which document versions). This acceptance is a **separate instrument from recording consent** ([CMP-002](01-nfr-and-compliance.spec.md)) and shall never be merged with it into a single control: the consent checkbox stands alone. When either document materially changes, acceptance is re-requested at next entry. The documents' legal text is owned by counsel; the platform's master contract with the customer org is executed outside the product.
**Verified by:** acceptance required at both gates; acceptance records with document versions retrievable; consent checkbox demonstrably distinct from terms acceptance in test.

## 3. v1 rollout tiers *(Amendment A, 2026-07-24)*

**What tiers, and what does not.** The product does not tier. The twelve-file specification set is unchanged by this section: no functional requirement is dropped, deferred, or softened, and the system stays destination-shaped — every tier runs the same software against the same contracts. What tiers is the **rollout**: the concurrency ceiling, the availability target, and the data-survival posture the platform is *committed to* while it is running at that rung. This amendment exists because the funded-launch posture is not affordable before revenue, and a target the budget cannot buy is a fiction, not a commitment. Writing the affordable posture down makes it an honest, verifiable requirement instead of an unspoken shortfall.

**This section is the single source of the tiered figures.** Every downstream artifact — infrastructure compositions, cost models, DR scenario tables, test plans, the rollout tracker — **cites the tier and its ID and never restates the numbers**, exactly as the [00 §8](00-overview.spec.md) referencing rule requires of `NFR-*` and `CMP-*` themselves. A tiered number appearing anywhere but this table is a specification defect.

### 3.1 The three tiers

| | **T1 — demo** | **T2 — pilot-lite** | **T3 — funded launch** |
|---|---|---|---|
| **What it is** | The product shown working to real people, and a first team practising on it for real | One or two real teams in sustained weekly use; revenue adjacent, not yet contracted | The committed product, sold under the commitments in §1 |
| **NFR-002 — concurrent live calls** | **≤ 5** | **≤ 15** | **unchanged — the committed figures in NFR-002** |
| **NFR-003 — availability** | **≈ 99 % monthly, best-effort** | **≈ 99 % monthly, best-effort** | **unchanged — the committed figure in NFR-003** |
| **Redundancy posture accepted** | **Single host accepted** — one machine runs every plane; losing it is a full outage until it is re-applied | **Single-AZ accepted** — managed services, no cross-AZ redundancy | Multi-AZ per plane, as designed |
| **Data RPO accepted** | **≤ 24 h** (nightly dump + object sync) | **≤ 24 h** | **unchanged — [data 05 §3](../data/05-backup-and-disaster-recovery.data.md)** |
| **Data RTO accepted** | **best-effort** — a procedure without a rehearsed clock | **best-effort** | **unchanged — [data 05 §3](../data/05-backup-and-disaster-recovery.data.md)**, at full-journey scope per Phase 7 |
| **Monthly attempt ceiling** | **≈ 200 attempts/month** — a *budget* ceiling, not a capacity one (owner ruling 2026-07-24, §3.5) | not capped; T2's cost is not the binding constraint | not capped |
| **Success criteria ([00 §4](00-overview.spec.md), A021)** | not measured | not measured | **measured — Tier 3 only** |
| **Availability claim made to any customer** | none | none | NFR-003, as committed |

Three readings that keep the table honest:

1. **"Best-effort" is a real state, not a softer number.** At T1 and T2 the ≈ 99 % figure is a planning expectation used to size and to watch — no error budget is defended, no availability commitment is made to any customer, and a month that misses it is a signal, not a breach. NFR-003's committed 99.5 % becomes binding at T3 and only at T3.
2. **This section states what is *accepted*; it does not redesign the mechanisms.** The backup, restore, and recovery machinery remains owned by [data 05](../data/05-backup-and-disaster-recovery.data.md) and Phase 7. What §3 adds is the business's acceptance of a weaker posture at the lower rungs — an acceptance that previously did not exist anywhere and was therefore being taken silently.
3. **Explicitly superseded for T1/T2 only:** [data 05 §3](../data/05-backup-and-disaster-recovery.data.md)'s honesty note that a 24 h RPO "is not accepted" was written when *launch* meant T3, and it remains correct at T3. At T1 and T2 a ≤ 24 h RPO **is** accepted, by this amendment, with the loss it implies stated plainly: a bad day can cost up to a day of attempts, grades, and hiring decisions. The [ADR-0022](../stack/adr/0022-postgresql-rls.md) PITR fallback it names is a T3 trigger, not a T1 one.
4. **The 24 h acceptance covers practice data. It does not cover irreversible external effects.** Training attempts are repeatable — a lost practice call is an annoyance. Hiring is not: a lost day can destroy the record behind a shortlist **already emailed to HR** ([E-4](00-overview.spec.md)), a report **already sent to a candidate** ([E-3](00-overview.spec.md)), and a candidate's consumed attempt they cannot simply retake ([FR-CND](23-candidate-flow.spec.md)) — leaving a decision that was communicated and evidence that no longer exists, which is a [CMP-001](01-nfr-and-compliance.spec.md) record problem as well as a trust one. **Therefore: while the Hiring product is in real use at T1 or T2, the deployment's data layer shall be one that provides point-in-time recovery.** At T1 that option is also the cheaper one, so this costs nothing to satisfy ([infra 01 §7.2](../infra/01-topology-and-networking.infra.md), [infra 04 §6](../infra/04-cost-model.infra.md)) — the requirement exists so the choice is never made by accident.

The deployment composition that realizes each tier, and its price, are Phase 7's ([infra README](../infra/README.md)); the rung currently in force is tracked in [LADDER.md](../LADDER.md).

### 3.2 Invariant across all tiers — never tiered

The following hold identically at T1, T2, and T3, and **no rollout tier may weaken any of them**:

| Invariant | Why it cannot tier |
|---|---|
| **NFR-001** — voice round-trip latency | It *is* the product. A slow buyer is not a cheap demo of a fast one; it is a different, worse product, and it would invalidate every impression the demo tier exists to create. |
| **NFR-004** — grading consistency | A grade that moves when re-run is not a grade. Inconsistency at any tier destroys the evidence base the product sells. |
| **NFR-005** — grading turnaround | Turnaround is a coaching-loop property, not a capacity property; it is bounded by queue work that a smaller tier makes *easier*, not harder. |
| **NFR-006** — unit cost | The tiers exist to protect a budget. Relaxing the per-attempt cost ceiling at the tier that has the least money would defeat the amendment's own purpose. |
| **NFR-007** — browser support | Users arrive on the browser they own, at every tier. |
| **Every `CMP-*`** — PDPL, consent, job-relevance, tenant isolation, terms acceptance | Compliance is not a function of scale, funding, or audience. A demo processes real people's voices and real personal data; the legal obligations attach on the first call, not on the first invoice. **No tier grants a compliance concession, and none may be read into one.** |

**One structural rule protects all of the above: there is no tier-conditional application code.** The tier is expressed entirely in infrastructure composition ([ADR-0051](../infra/adr/0051-tiered-deployment-compositions.md)) and configuration values — limits, plan keys, schedules — and **never in a branch that changes product behavior**. A conditional in application code keyed on the tier is a defect, not an optimization: it would make the demo stop demonstrating the product, and it would leave the Tier-3 path carrying branches that no tier has ever exercised. Phase 9's Definition of Done and Phase 13's reviews enforce this.

### 3.3 Promotion triggers *(ratified by the owner 2026-07-24)*

Tiers change on a stated trigger, not on mood.

| ID | Transition | Trigger — any one suffices |
|---|---|---|
| **PT-1** | **T1 → T2** | (a) a **second real team** onboards; **or** (b) sustained demand **> 300 attempts/month**; **or** (c) a ceiling the T1 recipe rests on is reached in a normal month — the free vendor allowances, or the T1 budget cap itself ([infra 04 §6.3](../infra/04-cost-model.infra.md)) |
| **PT-2** | **T2 → T3** | (a) the **first paying org signs**; **or** (b) owner election |

**PT-1(b) is measured on *demand*, not on served volume** — attempts requested, including any refused once the T1 budget cap has bitten. This reading is load-bearing: T1's monthly attempt ceiling (§3.1) sits *below* 300, so counting only completed attempts would let the cap suppress the very signal that should trigger the promotion. A ceiling that hides the demand it is throttling is a ceiling that keeps you on the wrong rung.

Two rules that go with them:

- **Promotion is deliberate and one-way in v1.** Climbing a rung is a planned change with a checklist ([LADDER.md](../LADDER.md)), never a side effect of a busy week. Falling back down a rung is an incident plus an owner decision, not a routine lever.
- **A trigger fires the decision, not the migration.** When a trigger fires, the promotion becomes a standing owner agenda item; the rung only changes when its climb checklist is complete.
- **The rung is re-affirmed, never assumed.** A tier acceptance has no natural expiry, and nothing breaks loudly when a best-effort target quietly stops being honest — so **the current rung is reviewed on the same monthly cadence as the cost reconciliation** ([infra 04 §5](../infra/04-cost-model.infra.md)), and a fired-but-unactioned trigger is **logged as an explicit deferral** in [LADDER.md](../LADDER.md) rather than passed over in silence. Three consecutive deferrals of the same trigger escalate to a decision the owner must record either way. This rule exists because the most likely failure of this amendment is not a bad tier — it is a good tier that outlives its own acceptance.

**Tiering does not shortcut the lifecycle.** These tiers change *what gets built and what it is committed to*, never *when the process permits building it*. T1 is still gated by every phase between here and Phase 15, and the Pre-T1 checklist in [LADDER.md](../LADDER.md) is not a substitute for them. A cheap rung is not a fast lane.

### 3.4 Verification (Phase 14's instruction)

Phase 14 verifies **the tier being shipped**:

- The tiered targets are asserted at the **tier in force** — the load test runs at that tier's NFR-002 ceiling, and its availability and data-survival evidence is gathered against that tier's row in §3.1.
- The **§3.2 invariants are verified in full at every tier**, without discount. NFR-001 is asserted at the tier's own concurrency, not waived because the tier is small; every `CMP-*` is verified before the first real person's first call, at T1.
- **An invariant needs a detector at the tier where it ships, not only a test at verification time.** NFR-001 and NFR-006 are the two most at risk on a shared single host and the two most invisible when they slip — a laggy buyer produces abandonment, not an error. Their instrumentation (per-turn latency; per-attempt cost) is therefore **live from the first deployed tier**, not deferred to the tier that can afford a full observability build.
- **Tier 3 verification gates the funded launch.** The committed NFR-002 and NFR-003 figures, and the [data 05 §3](../data/05-backup-and-disaster-recovery.data.md) RPO/RTO targets, are proven at Tier 3 conditions before any customer is sold the commitment in §1 — shipping at T1 or T2 verifies nothing about T3, and no T1/T2 evidence may be presented as satisfying it.

### 3.5 The T1 budget ceiling *(owner ruling, 2026-07-24)*

**The Tier-1 monthly cost is hard-capped at USD 120. When the honest arithmetic and the cap disagree, the volume gives way, not the cap** ([infra 04 §6.3](../infra/04-cost-model.infra.md) shows the arithmetic: ~⅔ of the T1 bill is per-attempt vendor usage, so ~200 attempts/month is what $120 buys). This is the one place where money, not capacity, sets a limit — and it is stated here because it is a tiered operating figure like any other.

**How it is enforced — and how it is deliberately *not*.** The ceiling is realized by the **per-vendor monthly spend caps the T1 recipe already mandates** ([infra 01 §7.2](../infra/01-topology-and-networking.infra.md)), sized to the ceiling. It is **not** a new product quota: no functional requirement is added, no interface changes, and no code branches on the tier — which §3.2 forbids and which would have made the demo a different product from the one it demonstrates. When the caps bite, calls fail to establish through the path the system already specifies for an unavailable capability ([FR-LIV-016](13-live-call.spec.md)), with the state the design already draws.

**The honest cost of this ruling, stated so it is a choice and not a discovery.** A spend cap is a *hard stop*, not a graceful throttle: the 201st attempt fails, and to the rep or candidate it is **indistinguishable from a real vendor outage**. Two consequences follow and both are binding:

1. **A warning threshold at 80 % of each cap is mandatory** — the month's remaining headroom must be a known quantity, so that stopping, topping up, or promoting is a decision taken in advance rather than a surprise experienced by a user mid-drill.
2. **Hitting the ceiling is a PT-1 trigger** (§3.3, limb c), not merely a spending event. The rung that cannot serve the demand it has is the rung you have outgrown.

## 4. Referencing rule

Feature files cite these IDs where a behavior depends on them (e.g. the live call cites NFR-001/NFR-002; grading cites NFR-004/NFR-005; identity and candidate flows cite CMP-002/CMP-004). The numbers above are the single source of truth; any restatement elsewhere is a specification defect. The same rule governs §3's tiered figures — cite the tier, never the number.
