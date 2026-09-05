# 23 — Candidate Flow

**Responsibility:** the candidate's journey — entry by invite link, pre-flight, the assessment plan, per-drill progression, completion, continuity across interruptions and re-entry, and the candidate's report email. It does not own the token mechanics ([10](10-identity-and-access.spec.md)), brief and call behavior ([13](13-live-call.spec.md)), grading ([14](14-scoring-and-review.spec.md)), or the position and assessment themselves ([22](22-hiring-manager.spec.md)).

## Overview

The candidate's experience is a single sitting in the browser: click the emailed link, pass a hardware check, consent, then take the drills one by one in their set order — each with a brief, a live call, and a return to the plan — ending at a terminal thank-you. No account, no score, no dashboard: the candidate sees their progress, never their evaluation. The interface presents itself in minimal assessment chrome, without the product's navigation.

## Functional Requirements

### FR-CND-001: Entry `[Must]`
When a candidate opens a valid invite link ([FR-IDA-012](10-identity-and-access.spec.md)), the system shall present their pre-flight: a personalized welcome naming them, the position, and the hiring org; how the assessment works; and preparation guidance (quiet room, headphones, no pausing mid-drill).

### FR-CND-002: Pre-flight checks `[Must]`
Before the assessment can start, the system shall verify the candidate's hardware and environment — microphone (with a live input indicator), audio output (with a test tone), and browser and connection suitability ([NFR-007](01-nfr-and-compliance.spec.md)) — each check passing or failing independently; while any check fails, the start shall stay blocked, with per-check guidance and retry.

### FR-CND-003: Consent and terms `[Must]`
The pre-flight shall capture the candidate's recording consent ([CMP-002](01-nfr-and-compliance.spec.md)) and Terms-of-Use / Privacy-Notice acceptance ([CMP-005](01-nfr-and-compliance.spec.md)) before the assessment can start, and shall display the assessment's language as set by the hiring team.

### FR-CND-004: The plan `[Must]`
The assessment plan shall present the position's drills in their frozen stage order ([FR-HIR-005](22-hiring-manager.spec.md)), each stage marked completed, next, or upcoming, with overall progress; only the next stage can open its brief, and stages are taken strictly in order.

### FR-CND-005: Brief and reference `[Must]`
A stage's brief renders per the standard brief ([FR-LIV-001](13-live-call.spec.md)) in candidate chrome, with the drill's product reference available ([FR-KNW-009](11-knowledge.spec.md)); starting the call enters the live call ([13](13-live-call.spec.md)).

### FR-CND-006: In-call presentation `[Should]`
During a candidate's call, the assessment chrome shall show the stage position (stage N of M), and the in-call controls are limited to mute and end-call ([13](13-live-call.spec.md)).

### FR-CND-007: Progression `[Must]`
When a candidate's call completes, the system shall return them to the plan with that stage completed and the next unlocked; the candidate shall see no score, review, or evaluation of any kind at any point ([FR-SCR-018](14-scoring-and-review.spec.md)).

### FR-CND-008: Completion `[Must]`
When the last stage completes, the system shall present the terminal completion screen — stages completed, total time, and submission confirmation — with no further actions; from then on the token admits only this state ([FR-IDA-013](10-identity-and-access.spec.md)).

### FR-CND-009: Re-entry `[Must]`
While the token is valid, when a candidate leaves and returns, the system shall resume them at the plan with completed stages locked and the next stage available — including across devices, subject to the single-active-call rule ([FR-LIV-005](13-live-call.spec.md)).

### FR-CND-010: Interruption and the one restart `[Must]`
When a candidate's call is interrupted ([FR-LIV-014](13-live-call.spec.md)), the stage returns to next and may be **restarted once**, the attempt carrying a restart flag surfaced to the manager ([FR-SCR-014](14-scoring-and-review.spec.md), [FR-HIR-011](22-hiring-manager.spec.md)); if the restarted call is interrupted again, the stage is consumed — it closes incomplete, and the candidate continues with the remaining stages ([FR-HIR-016](22-hiring-manager.spec.md)).

### FR-CND-011: Expiry mid-assessment `[Must]`
When a candidate's token expires with the assessment unfinished, completed stages remain submitted and graded, re-entry is refused with an expiry explanation ([FR-IDA-013](10-identity-and-access.spec.md)), and the candidate presents to the manager as incomplete ([FR-HIR-016](22-hiring-manager.spec.md)); a resent invite ([FR-HIR-009](22-hiring-manager.spec.md)) resumes the same assessment state.

### FR-CND-012: The candidate's report email `[Must]`
Per the position's candidate-report policy ([FR-HIR-003](22-hiring-manager.spec.md)), the system shall email the candidate their report (E-3) — the concealment-safe PDF ([FR-HIR-012](22-hiring-manager.spec.md)): on completion where the policy sends right after finishing; upon a reject decision where the policy sends only to rejected candidates; never where the policy withholds it.

## NFR & compliance references

Consent and terms at the gate: [CMP-002](01-nfr-and-compliance.spec.md), [CMP-005](01-nfr-and-compliance.spec.md). Candidate personal data and erasure/export: [CMP-001](01-nfr-and-compliance.spec.md). Token scoping: [FR-IDA-011](10-identity-and-access.spec.md) under [CMP-004](01-nfr-and-compliance.spec.md). Journey availability: [NFR-003](01-nfr-and-compliance.spec.md); browser/microphone support: [NFR-007](01-nfr-and-compliance.spec.md).

## Acceptance Criteria

### AC-CND-001: Blocked until ready
Given a candidate whose microphone check fails
When they attempt to start the assessment
Then the start is blocked with microphone guidance and a retry
And after the check passes and consent and terms are captured, the assessment starts.

### AC-CND-002: Strict order
Given an assessment of four stages with the first completed
When the candidate views the plan
Then stage two alone offers its brief, three and four are upcoming
And no path opens a later stage early.

### AC-CND-003: No evaluation ever visible
Given a candidate completing every stage
When they inspect everything shown to them from entry to completion
Then no score, grade, review, or evaluative content appears anywhere.

### AC-CND-004: One restart, flagged
Given a candidate's call dropped mid-stage
When they restart the stage and complete it
Then the completed attempt is graded and carries a visible restart flag in the manager's report
And a second interruption on the same stage would close it incomplete and unlock the next.

### AC-CND-005: Re-entry resumes
Given a candidate who closed the browser after two of four stages
When they reopen the valid link the next day
Then the plan shows two completed stages locked and stage three as next.

### AC-CND-006: Report email follows policy
Given three positions with the three report policies and a rejected candidate in each
When completion and decisions occur
Then the send-after-finish candidate got their PDF at completion, the only-rejected candidate got theirs upon rejection, and the withheld candidate received nothing.

## Provenance

- Entry and pre-flight: walkthrough C1. Plan and stage states: C1.5. Brief: C1.5b. Reference: C1.6. In-call chrome and controls: C2. Completion: C3.
- The one-restart-with-flag policy, second-interruption consumption, re-entry semantics, and expiry-mid-assessment handling: product decisions recorded in this file.
