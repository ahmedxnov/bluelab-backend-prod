# 21 — Training: Manager Surface

**Responsibility:** the manager's coaching surface — the team dashboard and its computations (the rating formulas), rep deep dives, replays, the drill catalog and per-drill views, and assignment. It does not own drill content or lifecycle ([12](12-drill-lifecycle.spec.md)), reviews and statistics primitives ([14](14-scoring-and-review.spec.md)), the team relationship itself ([10](10-identity-and-access.spec.md)), or hiring ([22](22-hiring-manager.spec.md)).

## Overview

The manager's surface answers three questions in order: how is my team doing (dashboard), where exactly does it hurt (gap analysis, deep dives, replays), and what do I do about it (author and assign drills). Everything here operates on the manager's own team ([FR-IDA-009](10-identity-and-access.spec.md)) — and unlike the rep surface, comparison is the point: rosters, leaderboards, and gaps are how a coach sees.

## Functional Requirements

### The numbers

### FR-TRM-001: Team scope `[Must]`
Every surface, list, and computation in this file shall operate exclusively on the manager's own team — its reps, its drills, its attempts ([FR-IDA-009](10-identity-and-access.spec.md)).

### FR-TRM-002: Rating formulas `[Must]`
The **counted pool** is the team's graded attempts on team drills — excluding self-authored practice ([FR-TRP-010](20-training-rep.spec.md)), test calls ([FR-DRL-012](12-drill-lifecycle.spec.md)), and non-graded attempts ([FR-SCR-014](14-scoring-and-review.spec.md)). From it: a rep's **monthly rating** (overall, and per call type) is the mean of their counted attempts' overall scores in the calendar month; a rep's **trend** is the change versus the prior month; the **team average** is the mean of member monthly ratings over members with at least one counted attempt that month.

### FR-TRM-003: Performance tiers `[Must]`
Within a month, reps with at least one counted attempt shall tier by monthly rating: the top 20% are **top performers**, the bottom 20% **need coaching**, the rest are mid — each 20% rounded to the nearest whole rep with a minimum of one, and tiering suppressed altogether while fewer than five reps hold counted attempts (too few for the split to carry meaning, leaving ratings shown untiered) — driving the dashboard's tier counts and roster pills.

### FR-TRM-004: Dashboard `[Must]`
The dashboard shall present the current month: the team average with its delta versus the prior month, the top-performer and needs-coaching counts, and the **gap analysis** — per call type, the top performers' average versus the bottom half's average, the gap being their difference, rows sorted widest gap first with severity marked (≥ 2.0 high, ≥ 1.0 moderate).

### FR-TRM-005: Roster `[Must]`
Below the dashboard, the roster shall list every team rep sorted by monthly rating: tier pill (FR-TRM-003), rating, counted attempts this month, their strongest and weakest call types — each row opening that rep's deep dive.

### FR-TRM-006: Rep deep dive `[Must]`
A rep's deep dive shall present their monthly rating with trend, their per-call-type profile, and their recent counted attempts — each opening its replay (FR-TRM-007).

### Seeing the work

### FR-TRM-007: Replay `[Must]`
The manager shall open any counted team attempt's review read-only ([FR-SCR-018](14-scoring-and-review.spec.md)) in replay chrome naming whose attempt it is and its number, with rubric weights visible ([FR-SCR-017](14-scoring-and-review.spec.md)).

### FR-TRM-008: Drill catalog `[Must]`
The manager shall see their team's drills — published and drafts — with status, average score, attempt count, and last update; opening a published drill leads to its detail (FR-TRM-009), and new drills are authored via the standard flow ([12](12-drill-lifecycle.spec.md)).

### FR-TRM-009: Drill detail `[Must]`
A published drill's detail shall present its rollup ([FR-SCR-016](14-scoring-and-review.spec.md)) — team average on the drill, reps practiced of eligible, total attempts — and a leaderboard of reps ranked by best score with last-attempt recency and a replay per rep, plus paths to the drill's read-only details (FR-TRM-010) and its assignment (FR-TRM-011).

### FR-TRM-010: Drill details view `[Must]`
The drill's details view shall present, read-only, its full basis — call setup, scenario, challenges and hidden motives, and rubric with weights (the author-side view, [FR-SCR-017](14-scoring-and-review.spec.md)) — with a test call available ([FR-DRL-012](12-drill-lifecycle.spec.md)) and archiving ([FR-DRL-016](12-drill-lifecycle.spec.md)).

### Assignment

### FR-TRM-011: Assign `[Must]`
The manager shall assign any published team drill to one or more of their reps with a due date and an attempts allowance; the assignment appears in each recipient's library with its due date and allowance ([FR-TRP-006](20-training-rep.spec.md)). Assignment is in-app only — it sends no email ([00 §6](00-overview.spec.md)).

### FR-TRM-012: Re-assign `[Must]`
When the manager re-assigns an already-assigned drill, the system shall update its recipients and due date and grant recipients a fresh attempts allowance — never duplicating the drill.

### FR-TRM-013: Assignment editing `[Must]`
The manager shall edit a published drill's assignment — recipients, due date, allowance — at any time, taking effect immediately and touching nothing of the drill's frozen content ([FR-DRL-015](12-drill-lifecycle.spec.md)).

### FR-TRM-014: Quick picks `[Could]`
Recipient selection shall offer computed cohorts alongside individual reps. All cohorts contain only active reps on the manager's own team. For the selected org-local calendar month: **bottom half by rating** is the V-4-ranked, rated population only, where `rated_reps >= 5` and `rank_desc > ceil(rated_reps / 2.0)` (otherwise empty); **rating below 6.0** is every rep with a V-2 monthly rating strictly below 6.0; and **fewer than 5 counted attempts** is every rep with fewer than five V-1 attempts, including zero. **Newest joiners** is at most five active reps, ordered by `account.created_at DESC, account.id ASC`. The first three cohorts order by their qualifying metric then account id: bottom-half and below-6 ascending rating, fewer-than-5 ascending count; all ties break by account id ascending.

## NFR & compliance references

Team boundary: [FR-IDA-009](10-identity-and-access.spec.md) under [CMP-004](01-nfr-and-compliance.spec.md). Reps' personal data in team views: [CMP-001](01-nfr-and-compliance.spec.md). Availability and platform: [NFR-003](01-nfr-and-compliance.spec.md), [NFR-007](01-nfr-and-compliance.spec.md).

## Acceptance Criteria

### AC-TRM-001: The formulas hold
Given a team of five reps with known graded attempts, one rep also holding self-authored attempts and one test call in the period
When the dashboard renders
Then every rating, the team average, tier counts, and gap rows match hand-computation from FR-TRM-002/003/004 — with the self-authored attempts and test call counted nowhere.

### AC-TRM-002: Gap analysis orders by pain
Given call types whose top-vs-bottom gaps are 2.3, 1.4, and 0.6
When the gap analysis renders
Then rows appear in that order, marked high, moderate, and unmarked respectively.

### AC-TRM-003: Replay is read-only truth
Given a rep's graded attempt
When the manager opens it from the deep dive
Then the full review renders identically to the rep's view plus weights, in chrome naming the rep and attempt number
And no control can alter the scorecard.

### AC-TRM-004: Leaderboard ranks best scores
Given four reps having attempted a drill with best scores 8.1, 7.2, 6.9, 5.0
When its detail renders
Then the leaderboard lists them in that order, tier-colored, each with a working replay.

### AC-TRM-005: Re-assignment refreshes
Given a rep who exhausted a drill's allowance of 2
When the manager re-assigns the drill to the same rep with a new due date
Then the rep's library shows the drill unlocked with a fresh allowance and the new due date
And the team has exactly one instance of the drill.

### AC-TRM-006: Nothing crosses teams
Given two managers in one org
When manager A's dashboard, catalog, and rosters render
Then nothing of manager B's team — reps, drills, attempts, or statistics — appears in any of them ([AC-IDA-007](10-identity-and-access.spec.md)).

## Provenance

- Dashboard, gap analysis, roster: walkthrough B1. Deep dive: B4b. Replay chrome: B5. Catalog: B6. Drill detail and leaderboard: B2. Details/assign split views: B8. Assignment steps and quick-pick cohorts: B7 (steps 3–4).
- The tier thresholds (top/bottom 20%), the counted-pool definition, and re-assignment granting a fresh allowance: product decisions recorded in this file — the wireframes display these views without defining their arithmetic.
