# BlueLab v1 — Requirements Overview

**Stage:** DEFINE · **Phase:** 1 — Requirements Engineering · **Status:** **complete** — gate passed;
this file is the set's manifest and status line.

**Amendments (in force, newest first):**

- **A-3 · 2026-07-25 — security requirements fed back.** The security & compliance design
  ([security/](../security/README.md)) produced testable, ID'd security requirements, added to the set as a new
  file, [02-security-requirements.spec.md](02-security-requirements.spec.md) (family `SEC-*`, `SEC-001…SEC-041`).
  **No functional requirement, the scope line (§3), or any tiered figure changed** — the `SEC-*` make the
  system's security posture explicit and verifiable; they add no product capability, so the §3 scope line holds
  unchanged. Design revisions the analysis forced are routed to their owning sets for re-approval
  ([security/08 §3](../security/08-security-requirements-and-routebacks.security.md)); two are **contingent on
  counsel's answers** to the [01 §2](01-nfr-and-compliance.spec.md) compliance obligations (erasure
  interpretation and consent-record survival). Manifest updated in §7.
- **A-2 · 2026-07-24 — v1 rollout tiers.** Through the Phase 1 gate as a requirements amendment, at the
  owner's direction: v1's **rollout** is tiered (T1 demo · T2 pilot-lite · T3 funded launch), because
  the committed operating posture is not affordable before revenue. **NFR-002**, **NFR-003**, and the
  accepted data RPO/RTO become tiered; **NFR-001, NFR-004, NFR-005, NFR-006, NFR-007 and every `CMP-*`
  are invariant across tiers** and were not touched. No functional requirement changed, and the scope
  line in §3 did not move. The tiered figures, the invariants, the promotion triggers, and Phase 14's
  per-tier verification rule are stated once in [01 §3](01-nfr-and-compliance.spec.md); the current rung
  is tracked in [LADDER.md](../LADDER.md). Companion amendment in Phase 7 ([infra README](../infra/README.md))
  adds the Tier-1 deployment recipe and its price. **Gate: `the-fool` pre-mortem run 2026-07-24
  (FS-1…FS-7); two findings applied** — a PITR-capable data layer is required while Hiring is in real use
  ([01 §3.1](01-nfr-and-compliance.spec.md) reading 4), and the current rung is re-affirmed monthly with
  deferrals logged ([01 §3.3](01-nfr-and-compliance.spec.md)) — **owner sign-off recorded 2026-07-24**:
  amendment accepted, promotion triggers **PT-1/PT-2 ratified**, and the Tier-1 budget ceiling ruled
  ([01 §3.5](01-nfr-and-compliance.spec.md)). Conditions C-4…C-6 bind Phases 8, 9, 11, 13
  ([infra README](../infra/README.md)).
- **A-1 · 2026-07-19 — FR-LIV-005 binding.** One live call binds on the **participant**, not on a user
  or candidate token ([13](13-live-call.spec.md)); AC-LIV-008 added.

**Responsibility:** the shared foundation only — what the product is, who uses it, the v1 scope line, success criteria, constraints, conventions, and the index of the specification set. This document defines **no system behavior**; every requirement lives in the file that owns it (see the manifest, §7).

---

## 1. The product

BlueLab is a voice-roleplay platform for sales teams, sold as multi-tenant SaaS. One AI capability — a simulated buyer on a live voice call, graded against a per-drill rubric — powers two products:

- **BlueLab Training** — reps practice short voice drills against AI buyers with hidden motives and receive coaching reviews; managers author and assign drills, track the team by call type, and maintain the product facts that ground every grade.
- **BlueLab Hiring** — managers build position-specific assessments from the same drills; candidates take them in the browser through a no-login invite link; the AI grades each call and the manager decides, forwarding approved candidates to HR with full reports.

The platform is **vertical-configurable**: buyer personas, product facts, and drill content are tenant data, not fixed content. v1 ships configured for its first vertical — Egyptian B2B insurance (the seed configuration: an insurer selling Group Medical) — and the requirements in this set are written generically so any comparable sales vertical can be configured without new specification.

## 2. Actors

| Actor | Who they are | Access model (owned by [10](10-identity-and-access.spec.md)) |
|---|---|---|
| **Manager** | Customer-org sales manager. Authors and assigns drills, coaches the team, runs hiring end-to-end. | Provisioned account (email + password) |
| **Rep** | Customer-org salesperson. Practices drills, reviews their own results, authors private drills. | Provisioned account (email + password) |
| **Candidate** | External job applicant. Takes one assessment in one sitting. | Tokenized invite link — no account |
| **BlueLab Internal Operations** | BlueLab's own staff. Provisions and deactivates customer accounts, resolves escalated system faults. | Internal access — not a customer role |

HR appears only as an **email recipient** of the hiring shortlist; HR is not a user of the system.

## 3. v1 scope

**v1 comprises exactly the capabilities specified in this twelve-file set — the complete behavior of both products as written here, and nothing beyond it.** This declaration is the scope line: a capability is in v1 if and only if a requirement in this set states it.

The shape of v1, in five statements:

1. **Both products ship together** — Training and Hiring, complete as specified.
2. **Calls and grading are in Egyptian Arabic** — the live roleplay and the AI evaluation operate in Egyptian Arabic; the application interface and all written coaching commentary are in English, with spoken quotes rendered verbatim in Arabic.
3. **Desktop-browser web application** — all three user-facing experiences target current desktop browsers.
4. **Multi-tenant from day one** — multiple customer orgs, with tenant isolation as a compliance requirement ([CMP-004](01-nfr-and-compliance.spec.md)).
5. **Operator-mediated access** — BlueLab Internal Operations provisions every account; candidates enter by tokenized link only.

**Scope note — the rollout is tiered; the scope line is not.** v1 ships in three rollout tiers (T1 demo · T2 pilot-lite · T3 funded launch), defined once in [01 §3](01-nfr-and-compliance.spec.md). Tiers move only the **operating posture** — concurrency ceiling, availability target, accepted data RPO/RTO — and never the scope line above: every tier runs the complete twelve-file capability set specified here, and every tier carries every compliance requirement in full. A capability is in v1 if and only if a requirement in this set states it, at every tier alike.

## 4. Success criteria

Measured after launch; these define what "v1 succeeded" means.

| # | Criterion | Target |
|---|---|---|
| S-1 | **Training north star** — median rep's overall rating improvement within their first month of active practice | ≥ +1.0 points (of 10) |
| S-2 | Training support — assigned drills completed by due date | ≥ 70% |
| S-3 | Training support — practice cadence per active rep | ≥ 2 drills/week |
| S-4 | **Hiring north star** — screen precision: AI-approved candidates who pass the subsequent human interview | ≥ 80% |
| S-5 | **Hiring north star** — reduction in time-to-shortlist vs. manual screening | ≥ 50% |
| S-6 | Hiring support — invited candidates who complete the assessment | ≥ 85% |
| S-7 | Platform — pilot customer orgs live in the first quarter after launch | ≥ 3, of which ≥ 1 renews or expands |

S-4 and S-5 are measured operationally by BlueLab during pilots; the product itself carries no instrumentation for them.

**These criteria are measured at Tier 3 only** ([01 §3](01-nfr-and-compliance.spec.md)) — they define what "v1 succeeded" means for the funded launch. The lower rungs are not judged against them: a demo-tier month that misses S-2 or S-7 says nothing about the product, because the population, the load, and the commercial motion those targets assume do not exist yet.

## 5. Constraints

1. **No technology is pre-decided.** Every stack choice is made downstream with evidence. One recorded leaning for that evaluation: real-time voice I/O is expected to be a procured capability (with Egyptian Arabic quality as a selection criterion), not built in-house.
2. **The design wireframes are behavioral reference only.** Their code is not a seed; the walkthrough documents describe intended behavior, and this specification supersedes them wherever they differ.
3. **Compliance posture** — Egypt-first, privacy-by-design; binding requirements in [01](01-nfr-and-compliance.spec.md). GDPR-readiness guides design but is not a v1 certification.

## 6. Transactional email inventory

The complete set of emails the system sends — five, owned where listed; no others exist in v1.

| # | Email | Owner |
|---|---|---|
| E-1 | Account credentials (on provisioning) and password reset | [10](10-identity-and-access.spec.md) |
| E-2 | Candidate invite, and invite resend | [22](22-hiring-manager.spec.md) |
| E-3 | Candidate's own report (per the position's report policy) | [23](23-candidate-flow.spec.md) |
| E-4 | Shortlist to HR, with report PDFs attached | [22](22-hiring-manager.spec.md) |
| E-5 | Manager completion notification (per the position's toggle) | [22](22-hiring-manager.spec.md) |

## 7. The specification set — manifest

Twelve files at Phase 1 close, plus **[02-security-requirements.spec.md](02-security-requirements.spec.md)**
added by Amendment A-3 — thirteen in force. A handoff of this set is complete only if all thirteen are present.

| File | Owns |
|---|---|
| **00-overview.spec.md** (this file) | Foundation: product, actors, scope, success criteria, constraints, conventions, this manifest |
| **[glossary.md](glossary.md)** | Canonical vocabulary — terms only, no rules |
| **[01-nfr-and-compliance.spec.md](01-nfr-and-compliance.spec.md)** | Every measurable quality target (`NFR-*`) and every compliance requirement (`CMP-*`) |
| **[02-security-requirements.spec.md](02-security-requirements.spec.md)** *(Amendment A-3)* | Every testable security requirement (`SEC-*`), fed back from the security & compliance design |
| **[10-identity-and-access.spec.md](10-identity-and-access.spec.md)** | `FR-IDA-*` — provisioning, authentication, roles, org membership, candidate token access, offboarding |
| **[11-knowledge.spec.md](11-knowledge.spec.md)** | `FR-KNW-*` — product documents and facts, publish flow, the grounding rule, the participant-facing reference |
| **[12-drill-lifecycle.spec.md](12-drill-lifecycle.spec.md)** | `FR-DRL-*` — call-type taxonomy, authoring inputs, AI generation, rubric, test calls, publish and immutability |
| **[13-live-call.spec.md](13-live-call.spec.md)** | `FR-LIV-*` — the pre-call brief and the live call, for every participant type |
| **[14-scoring-and-review.spec.md](14-scoring-and-review.spec.md)** | `FR-SCR-*` — grading, the review artifact, attempt records, and all visibility/concealment rules |
| **[20-training-rep.spec.md](20-training-rep.spec.md)** | `FR-TRP-*` — the rep's product surface: progress, profile, library, history, self-authoring deltas |
| **[21-training-manager.spec.md](21-training-manager.spec.md)** | `FR-TRM-*` — the manager's coaching surface: dashboard, deep dives, drill management, assignment, team aggregation rules |
| **[22-hiring-manager.spec.md](22-hiring-manager.spec.md)** | `FR-HIR-*` — positions, assessments, candidate pipeline, reports and decisions, HR handoff |
| **[23-candidate-flow.spec.md](23-candidate-flow.spec.md)** | `FR-CND-*` — the candidate's journey from invite to completion |

## 8. Requirement conventions

- **Functional requirements** are written in **EARS** syntax (`While <state>, when <trigger>, the system shall <response>`), each with a unique ID from its file's family (table above) and a priority tag.
- **Priorities** are MoSCoW: `[Must]` — v1 does not ship without it · `[Should]` — important, slips last · `[Could]` — polish, slips first.
- **Acceptance criteria** are Given/When/Then, prefixed `AC-` in the same family as their file.
- **Ownership:** every concept is defined in exactly one file, declared in that file's opening Responsibility statement. Other files cite the owning ID; restating owned content in another file is a specification defect.
- **NFR and compliance targets** (`NFR-*`, `CMP-*`) are stated once, in [01](01-nfr-and-compliance.spec.md); all other files reference the IDs.
- **Rollout-tier figures** (`T1`/`T2`/`T3`, and the promotion triggers `PT-*`) are stated once, in [01 §3](01-nfr-and-compliance.spec.md); every other document — in this set and in every later phase — cites the tier, never the number.

## 9. How to read this set

Read this file, then the [glossary](glossary.md), then [01](01-nfr-and-compliance.spec.md). The 10-series files define behavior shared by both products; the 20-series files define each product's own surface on top of that shared behavior. Every file opens with its Responsibility statement; trust it — if content seems missing from a file, it is owned by the file its references point to. Each feature file ends with a Provenance section mapping its requirements to the design walkthrough screens they realize.
