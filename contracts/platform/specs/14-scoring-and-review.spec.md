# 14 — Scoring & Review

**Responsibility:** the system's judgment — grading, the scorecard and review artifact, the attempt record, per-drill statistics primitives, and **every visibility/concealment rule**. It does not own product-level aggregations (team ratings → [21](21-training-manager.spec.md); candidate overall and report → [22](22-hiring-manager.spec.md)), the call that produced the capture ([13](13-live-call.spec.md)), or the surfaces reviews appear in ([20](20-training-rep.spec.md)–[23](23-candidate-flow.spec.md)).

## Overview

When a call completes, the evaluator grades the call — its transcript and the participant's demeanor toward the buyer — against the frozen drill: its rubric and the facts it was built on, producing a scorecard, a coach takeaway, and evidence-anchored moments, composed into the review. Grading works from the derived transcript-and-demeanor signal, not the raw recording, which is there for people to listen back to. An attempt is graded exactly once and its result never changes. One concealment rule governs who sees what, everywhere: what a drill conceals, it conceals from non-authors forever.

## Functional Requirements

### Grading

### FR-SCR-001: Grading trigger `[Must]`
When a call completes (not interrupted, not a test call), the system shall grade it — targeting [NFR-005](01-nfr-and-compliance.spec.md).

### FR-SCR-002: Grading basis `[Must]`
Grading shall use only: the attempt's timestamped transcript (its words and turn structure — from which pace, talk-to-listen ratio, and pauses are available); the **demeanor signal** associated with the call — the participant's emotional tone toward the buyer as a behavior (e.g. aggressive, dismissive, warm, patient), carried as labels on transcript segments; and the **frozen drill** it was taken on — its rubric (how to score) together with the product facts it was grounded in (the answer key of correct values), both frozen at publish ([FR-DRL-014](12-drill-lifecycle.spec.md), [FR-KNW-007](11-knowledge.spec.md)). Grading assesses job-relevant conduct only, demeanor-as-conduct included and voice/speech identity traits excluded ([CMP-003](01-nfr-and-compliance.spec.md)); it consumes this derived transcript-and-demeanor signal, not the raw audio, and the recording serves human playback ([FR-SCR-011](14-scoring-and-review.spec.md)).

### FR-SCR-003: Graded once, immutable `[Must]`
The system shall grade an attempt exactly once and thereafter treat its scorecard as immutable; internal quality re-grades ([NFR-004](01-nfr-and-compliance.spec.md) verification) shall never alter a delivered scorecard.

### FR-SCR-004: Scorecard `[Must]`
A scorecard shall comprise: a per-dimension score and written note for every rubric dimension, and an overall score of 10 computed as the weight-weighted average of dimension scores.

### FR-SCR-005: Tier bands `[Must]`
Everywhere a score renders, the system shall band it identically: green ≥ 7.5, amber ≥ 5.5, red below 5.5.

### FR-SCR-006: Coach takeaway `[Must]`
Each graded attempt shall carry a single coach-takeaway paragraph naming what went well, the costliest slips, and what to do differently.

### FR-SCR-007: Moments `[Must]`
Each graded attempt shall carry moments — timestamped, severity-tagged (green/amber/red), each linked to the rubric dimension it evidences; amber and red moments shall each carry the participant's verbatim quote, the stronger line to try instead, and why it matters.

### FR-SCR-008: Commentary language `[Must]`
All generated commentary (takeaway, notes, moment guidance) shall be in English; quoted speech shall remain verbatim in the call language.

### FR-SCR-009: Grading failure `[Must]`
When grading fails, the system shall retry automatically; while failure persists, the attempt shall hold status **grading-pending**, its participant-facing state reading as preparation in progress, and the case shall surface to BlueLab Internal Operations. A completed call shall never be voided or lost to a grading fault.

### The review

### FR-SCR-010: Review composition `[Must]`
An attempt's review shall present: the drill and buyer identity, the attempt's duration, the overall score with tier band, the coach takeaway, the playback, the rubric breakdown (per-dimension score, name, bar, note — weight per FR-SCR-017), and the moments list.

### FR-SCR-011: Playback `[Must]`
The review's playback shall synchronize the recording, the transcript, and moment markers, opening pinned to the attempt's most coachable moment — its first red moment, else its first amber, else the call start — with per-moment seek from the moments list.

### FR-SCR-012: Moment filters and sort `[Should]`
The moments list shall filter by severity (with live counts, and an explicit empty state when a severity has none) and sort by importance (red → amber → green) or by time.

### FR-SCR-013: Playback degradation `[Should]`
While an attempt's recording is unavailable for playback, the system shall still render the review complete with transcript, scorecard, takeaway, and moments, mark the audio as unavailable, and surface the case to BlueLab Internal Operations; a review shall never fail to render for a playback-asset fault.

### The attempt record

### FR-SCR-014: Attempt record `[Must]`
Every attempt shall record: its drill, participant, org, start and end times, duration, status — in-progress → completed → graded (or grading-pending), or interrupted — and, for candidate attempts, the restart flag ([FR-CND](23-candidate-flow.spec.md)).

### FR-SCR-015: Per-participant drill statistics `[Must]`
For each participant and drill, the system shall derive from graded attempts: the attempt list (newest first), best score, latest score, average, and trend (latest versus first).

### FR-SCR-016: Per-drill statistics `[Must]`
For each published drill, the system shall derive from graded attempts: how many eligible participants attempted it, total attempts, average score, and each participant's best score.

### Visibility

### FR-SCR-017: The concealment rule `[Must]`
A drill's **concealed set** is its challenges, its hidden motives, and its rubric's weights. The system shall never expose any element of the concealed set to a non-author participant — before, during, or after any attempt, in any surface or delivered artifact, in both products, permanently. Authorship lifts concealment: a drill's author sees its full basis always; a manager sees the full basis of every drill in their team's catalog ([FR-IDA-009](10-identity-and-access.spec.md)). Dimension names, scores, and notes are shown to non-authors — but all coaching commentary rendered to a non-author (the takeaway, dimension notes, and moment guidance) shall coach the behavior to build without narrating any concealed element: it may say "probe for the buyer's alternatives," never "you missed their competing quote."

### FR-SCR-018: Review access `[Must]`
A rep shall access only their own reviews; a manager shall access every review of their team's attempts on team drills and of their positions' candidate attempts (rendered read-only, [FR-TRM](21-training-manager.spec.md) / [FR-HIR](22-hiring-manager.spec.md)) — a rep's self-authored practice stays private to its author ([FR-TRP-009](20-training-rep.spec.md)); a candidate shall access no review in the product — candidate results reach them only per their position's report policy ([FR-HIR](22-hiring-manager.spec.md), [FR-CND](23-candidate-flow.spec.md)), with concealed elements excluded per FR-SCR-017.

## NFR & compliance references

Turnaround: [NFR-005](01-nfr-and-compliance.spec.md). Consistency: [NFR-004](01-nfr-and-compliance.spec.md). Job-relevance and explainability: [CMP-003](01-nfr-and-compliance.spec.md). Org scoping: [CMP-004](01-nfr-and-compliance.spec.md). Erasure/export of attempt data: [CMP-001](01-nfr-and-compliance.spec.md).

## Acceptance Criteria

### AC-SCR-001: A graded review is complete
Given a rep completing a call on an assigned drill
When the review opens
Then it presents overall score with band, takeaway, playback pinned per FR-SCR-011, every rubric dimension with score and note, and the moments list
And every amber or red moment carries quote, try-instead, and why-it-matters
And no rubric weight is displayed.

### AC-SCR-002: Scorecard immutability
Given a graded attempt whose review has been viewed
When any internal process re-evaluates the same call
Then the delivered scorecard, takeaway, and moments are byte-identical on every later view.

### AC-SCR-003: Weights follow authorship
Given the same review viewed by three parties — the rep who took the assigned drill, the manager, and (for a self-authored drill) its rep author
When each opens the rubric breakdown
Then the manager and the author see per-dimension weights
And the non-author rep sees dimensions, scores, and notes with no weights.

### AC-SCR-004: Concealment survives the attempt
Given a rep who finished an assigned drill and a candidate who finished an assessment drill
When each inspects every post-call surface and artifact available to them
Then neither can discover the drill's challenges or hidden motives anywhere.

### AC-SCR-005: Grading failure holds the attempt
Given a completed call whose grading fails repeatedly
When the participant checks their attempt
Then it shows as being prepared, not failed or lost
And when BlueLab Internal Operations resolves it, the full review appears with the original call's content.

### AC-SCR-006: Statistics primitives
Given a rep with three graded attempts on one drill scoring 5.1, 6.4, 7.6
When their per-drill statistics render
Then best = 7.6, latest = 7.6, average = 6.4, trend = +2.5
And the attempt list is newest first.

### AC-SCR-007: Interrupted attempts never grade
Given an interrupted attempt
When any grading process runs
Then the attempt is never graded, appears in no statistics, and holds status interrupted.

## Provenance

- Review composition, playback pin, moments, filters/sort, weight visibility: walkthroughs A3 (both variants), B5, B18b. Statistics primitives: A3.5, B2. Tier bands: A0/A3/B1 color rules.
- Graded-once immutability, grading-failure policy, grounding-at-completion timing, and the permanent concealment boundary: product decisions recorded in this file.
