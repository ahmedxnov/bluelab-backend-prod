# 20 — Training: Rep Surface

**Responsibility:** the rep's product surface — My Progress, Profile, the drill library, per-drill attempt history, and the rep's self-authoring deltas including self-authored privacy. It does not own the drills themselves ([12](12-drill-lifecycle.spec.md)), briefs or calls ([13](13-live-call.spec.md)), reviews or statistics primitives ([14](14-scoring-and-review.spec.md)), assignment or the rating formulas ([21](21-training-manager.spec.md)).

## Overview

The rep's world is deliberately calm: a personal coaching home with no peer comparison anywhere, a single library holding everything they can practice — what their manager assigned, what their manager published, and what they built themselves — and, per drill, their own history and reviews. A rep can author private drills with the same AI-first flow their manager uses; nobody else ever sees them.

## Functional Requirements

### FR-TRP-001: My Progress `[Must]`
The rep's home shall present their current month as personal coaching: their monthly rating with its change versus the prior month and a rising/slipping indicator; a trend line across the month's weeks, scopable by a call-type filter (All or one call type); and one card per call type over the last 30 days — ordered weakest first, the weakest tagged, each score tier-banded ([FR-SCR-005](14-scoring-and-review.spec.md)).

### FR-TRP-002: One truth with the manager `[Must]`
Every rating, trend, and per-call-type value shown to the rep shall be computed by the same formulas and the same attempt pool as the manager's view of that rep ([FR-TRM-002](21-training-manager.spec.md)) — the two surfaces render the same numbers, reframed.

### FR-TRP-003: Coach feedback feed `[Should]`
The home shall carry a feed of system-generated coaching tips derived from the rep's graded attempts (e.g. a recurring weakness across recent calls), with an unread count; entries are visible only to the rep.

### FR-TRP-004: Profile `[Should]`
The rep's profile shall present their identity and lifetime figures: total completed drills and lifetime overall rating.

### FR-TRP-005: Badges `[Could]`
The system shall maintain a system-defined set of achievement badges, each earned by a stated rule; the profile shall show earned badges (with the rule that won them) and locked badges (with the goal to reach them). Badges are visible only to their owner and appear on no other surface.

### FR-TRP-006: The drill library `[Must]`
The rep's library shall present, as one grid, every drill the rep can practice: **assigned** drills (carrying their due date, and their attempts allowance state — [FR-TRM-011](21-training-manager.spec.md)), the rep's **self-authored** drills, and the team **library** drills (the manager's published drills, [FR-IDA-009](10-identity-and-access.spec.md)). Each card shall show the drill's call type, source, persona-derived label ([FR-DRL-004](12-drill-lifecycle.spec.md)), the rep's best score (or that it's unattempted), and lead to the drill's brief ([FR-LIV-001](13-live-call.spec.md)) and, when attempted, its history (FR-TRP-010).

### FR-TRP-007: Source tabs `[Must]`
The library shall filter by source — All, Assigned to me, Created by me — with live counts per tab.

### FR-TRP-008: Library refinements `[Could]`
The library shall additionally filter by call type and attempt status, and offer sort orders (e.g. recommended, recently assigned).

### FR-TRP-009: Self-authoring `[Must]`
A rep shall author drills through the standard authoring flow ([12](12-drill-lifecycle.spec.md)) without any assignment step; a published self-authored drill lands in the rep's Created-by-me source. A self-authored drill and every attempt on it are **private to their author**: no other rep, and not the manager, can see the drill, its attempts, or their reviews anywhere ([FR-SCR-018](14-scoring-and-review.spec.md)) — its brief carries the author's setup recap ([FR-LIV-002](13-live-call.spec.md)).

### FR-TRP-010: Self-authored practice is uncounted `[Must]`
Attempts on self-authored drills shall be excluded from every rating and statistic beyond the drill's own history — the rep's displayed ratings and trends (FR-TRP-001) and all team aggregation ([FR-TRM-002](21-training-manager.spec.md)) alike.

### FR-TRP-011: Attempt history `[Must]`
Per drill, the rep shall see their history built on the statistics primitives ([FR-SCR-015](14-scoring-and-review.spec.md)): best, latest, average, and trend; the attempt list newest first, each opening its review ([FR-SCR-010](14-scoring-and-review.spec.md)); and a practice-again path leading to the drill's brief.

### FR-TRP-012: Score progression chart `[Could]`
The history shall render the attempts as a score-progression chart, oldest first, each point tier-colored and opening that attempt's review.

### FR-TRP-013: Allowance lock `[Must]`
While an assigned drill's attempts allowance is exhausted, the library shall show it locked with its used/allowed count, and no call can start on it until the manager's re-assignment grants a fresh allowance ([FR-TRM-012](21-training-manager.spec.md)).

## NFR & compliance references

All surfaces: [NFR-007](01-nfr-and-compliance.spec.md) (platform), [NFR-003](01-nfr-and-compliance.spec.md) (availability). Rep personal data: [CMP-001](01-nfr-and-compliance.spec.md). No peer data ever renders here — the coaching-not-surveillance stance is enforced by composition: no requirement in this file surfaces another rep's identity or scores.

## Acceptance Criteria

### AC-TRP-001: Home reflects the month
Given a rep with graded attempts this month and last
When they open My Progress
Then their monthly rating, its delta versus last month, the weekly trend, and weakest-first call-type cards render
And filtering the trend to one call type redraws it to that scope
And no other rep's name or score appears anywhere.

### AC-TRP-002: Rep and manager see the same number
Given a rep's month of graded attempts
When the rep opens My Progress and the manager opens that rep's deep dive
Then both surfaces show the identical monthly rating and per-call-type values.

### AC-TRP-003: Library sources behave
Given a rep with two assigned drills, one self-authored drill, and a team library
When they switch source tabs
Then All, Assigned to me, and Created by me each show exactly their drills with correct counts
And assigned cards carry due dates and allowance state.

### AC-TRP-004: Self-authored privacy is total
Given a rep's published self-authored drill with three attempts
When the manager (or any other rep) searches every surface available to them
Then no trace of the drill, its attempts, or its reviews exists for them
And the rep's own displayed ratings are unaffected by those three attempts.

### AC-TRP-005: Allowance exhausts and returns
Given an assigned drill with 3 attempts allowed, all used
When the rep views it, it is locked showing 3/3 and no call can start
And when the manager re-assigns it, the rep can practice it again with a fresh allowance.

### AC-TRP-006: History adds up
Given four graded attempts on one drill
When the rep opens its history
Then best, latest, average, and trend match the primitives' math
And each listed attempt opens its own review.

## Provenance

- My Progress bands and chips: walkthrough A0 (the feed carries system tips; A0's manager-note entry awaits the deferred manager-notes capability). Profile and badges: A4. Library grid, tabs, card states: A1. History and progression chart: A3.5. Self-author flow and privacy: A1.7/A1.7b, A1.5h, A3 (self-authored variant).
- The allowance-lock display and the badge mechanism (system-defined rules; specific badge content is seed configuration, not specification): product decisions recorded in this file.
