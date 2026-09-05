# 12 — Drill Lifecycle

**Responsibility:** the drill from intent to retirement — the call-type taxonomy, authoring inputs, AI generation of scenario and rubric, rubric tuning, test calls, publishing, immutability, and archiving. It does not own who may author in which mode or assignment/attachment of drills ([20](20-training-rep.spec.md)/[21](21-training-manager.spec.md)/[22](22-hiring-manager.spec.md)), the brief's rendering ([13](13-live-call.spec.md)), or grading ([14](14-scoring-and-review.spec.md)).

## Overview

Authoring is AI-first: the author states the call's intent — call type, challenges, hidden motives — and the system generates the scenario (buyer persona, context, product references) and a matching rubric. The author tunes weights, optionally feel-checks the buyer on a test call, and publishes. From that moment the drill's content is frozen: every participant who ever takes it faces exactly the same buyer, situation, and scoring contract.

## Functional Requirements

### FR-DRL-001: Call-type taxonomy `[Must]`
The system shall classify every drill as exactly one call type — Discovery, Post Proposal, Renewal, or Upsell — and, for Discovery only, one lead type: Inbound Quote, Referral, or Cold Outreach.

### FR-DRL-002: Authoring inputs `[Must]`
When an author creates a drill, the system shall accept its intent as: a call type (with lead type if Discovery), one or more challenges, and one or more hidden motives — each selectable from a provided library or entered as custom text. Generation shall not run without at least one challenge and one hidden motive.

### FR-DRL-003: Drill language `[Must]`
Every v1 drill's call language shall be Egyptian Arabic ([00 §3](00-overview.spec.md)); the authoring surface shall present the language as fixed.

### FR-DRL-004: Scenario generation `[Must]`
When the author requests generation, the system shall produce the drill's scenario from its inputs: a buyer persona (a unique generated identity — name, role, company, meta facts), a situation context, and product references drawn exclusively from published facts ([FR-KNW-007](11-knowledge.spec.md)) — varying by call type (e.g. prior-proposal context for Post Proposal, account history for Renewal). The drill's identifying label everywhere shall derive from its generated persona (name · company); regeneration re-derives it.

### FR-DRL-005: Regenerate, never hand-edit `[Must]`
While a scenario exists, the system shall present it read-only; when the author changes inputs and/or requests regeneration, the system shall replace the entire scenario with a newly generated one.

### FR-DRL-006: Generation failure `[Must]`
When scenario or rubric generation fails, the system shall show the author the failure with a retry, shall substitute no fallback content, and shall not allow a drill to publish without successfully generated scenario and rubric ([interview policy]).

### FR-DRL-007: Job-relevance at generation `[Must]`
Per [CMP-003](01-nfr-and-compliance.spec.md), generated scenarios and rubrics shall concern job-relevant selling conduct only; when a custom challenge or motive entry is not job-relevant, the system shall reject it with the reason, before it can influence generation.

### FR-DRL-008: Rubric generation `[Must]`
When the author proceeds to the rubric step, the system shall present an AI-generated per-drill rubric: dimensions each carrying a name, a weight, and a rationale, derived from the drill's call type, challenges, motives, and the team's published facts (so dimensions concerning product accuracy are grounded in real product truth).

### FR-DRL-009: Rubric tuning `[Must]`
While the rubric is unpublished, the system shall let the author (a) adjust each dimension's weight and (b) delete a dimension, showing the live weight total for re-balancing. A dimension's name and rationale stay fixed as generated, and no dimension can be added or reshaped by hand — new or changed scored criteria come only from adjusting the drill's inputs and regenerating ([FR-DRL-011](12-drill-lifecycle.spec.md)), so every scored criterion stays within the job-relevance filter ([FR-DRL-007](12-drill-lifecycle.spec.md), [CMP-003](01-nfr-and-compliance.spec.md)).

### FR-DRL-010: Sum-to-100 gate `[Must]`
While the rubric's weights do not total exactly 100, the system shall block publishing and indicate the delta.

### FR-DRL-011: Rubric regeneration `[Must]`
When the author requests rubric regeneration, the system shall require an explicit confirmation that manual edits will be discarded, then replace the entire rubric with a newly generated one.

### FR-DRL-012: Test call `[Should]`
When an author starts a test call on a draft or published drill, the system shall run a live call against the drill's buyer ([FR-LIV](13-live-call.spec.md)) that is never graded and leaves no attempt record, no review, and no trace in any statistic.

### FR-DRL-013: Draft state `[Should]`
While a drill is unpublished, the system shall let the author save it as a draft at any completeness and resume authoring later; drafts are visible only in authoring surfaces, marked as drafts, and cannot be taken by anyone.

### FR-DRL-014: Publish `[Must]`
When the author publishes a drill (with a generated scenario and a rubric totaling 100), the system shall freeze — as the drill's immutable content — its scenario (with its persona-derived label), its rubric, its language, and a snapshot of the team's published facts it was grounded in (its answer key), and make it available per its mode's rules ([20](20-training-rep.spec.md)/[21](21-training-manager.spec.md)/[22](22-hiring-manager.spec.md)).

### FR-DRL-015: Immutability `[Must]`
While a drill is published, the system shall provide no path that alters its frozen content; changing its scenario, its rubric, or the facts it grades against means creating a new drill — including refreshing a drill to the team's updated facts, which is a republish, not an in-place edit.

### FR-DRL-016: Archive `[Should]`
When a manager archives a published drill, the system shall withdraw it from all availability — libraries, assessments' future use, and assignment, cancelling any outstanding assignments so the drill leaves its recipients' libraries — while preserving every existing attempt, review, and statistic that references it.

## NFR & compliance references

Generation and rubric content: [CMP-003](01-nfr-and-compliance.spec.md). Scenario grounding: [FR-KNW-007](11-knowledge.spec.md) under [CMP-004](01-nfr-and-compliance.spec.md) org scoping. Frozen-content comparability underpins [NFR-004](01-nfr-and-compliance.spec.md) and [CMP-003](01-nfr-and-compliance.spec.md)'s identical-assessment clause.

## Acceptance Criteria

### AC-DRL-001: Author end to end
Given a manager creating a drill with call type Post Proposal, two challenges, one custom motive
When they generate, review the scenario, tune one rubric weight to reach 100, and publish
Then the drill is published with the tuned rubric
And its scenario's product references match currently published facts.

### AC-DRL-002: Regeneration replaces
Given a generated scenario the author dislikes
When they add a challenge and regenerate
Then a wholly new scenario renders reflecting the added challenge
And no element of the prior scenario survives.

### AC-DRL-003: Publish blocked at 97
Given a rubric whose weights total 97
When the author attempts to publish
Then publishing is blocked with "+3 to balance" indicated
And after adjusting to exactly 100 publishing proceeds.

### AC-DRL-004: Non-job-relevant custom entry
Given an author typing a custom motive that targets a personal characteristic rather than call conduct
When they commit the entry
Then it is rejected with a job-relevance explanation
And it never appears among the drill's motives nor influences generation.

### AC-DRL-005: Test call leaves nothing
Given an author completing a test call on a draft drill
When they finish and inspect every library, history, review, and statistic in the org
Then no record of the test call exists anywhere.

### AC-DRL-006: Immutability
Given a published drill
When any user inspects it in any surface
Then no control exists that could modify its scenario, its persona-derived label, its rubric, or language.

### AC-DRL-007: Archive preserves history
Given a published drill with 12 attempts and a team leaderboard
When a manager archives it
Then it can no longer be taken or newly assigned/attached
And all 12 attempts, their reviews, and its statistics remain intact and viewable.

### AC-DRL-008: Delete a dimension and re-balance
Given a generated rubric of five dimensions summing to 100
When the author deletes one dimension
Then it no longer appears in the rubric
And publishing is blocked until the remaining weights are re-balanced to exactly 100
And no affordance exists to add a dimension or to edit a dimension's name or rationale.

## Provenance

- Authoring inputs, generation, review block: walkthroughs B7, B14, A1.7. Rubric review and tuning: B7b, B14b, A1.7b. Test call: B7/B14 action bars, B8 footer. Immutability rationale and read-only details: B8 (Details view). Library/status states: B6.
- Generation-failure policy and test-call ephemerality: product decisions recorded in this file. Drill identity derives from the persona (refining the wireframes' editable name field), and generation requires at least one challenge and one motive.
- Author rubric edits are limited to adjusting weights and deleting dimensions — not editing rationale text, which the wireframe's inline editor offered (B7b/B14b) — so every scored criterion stays inside the generation-time job-relevance filter.
