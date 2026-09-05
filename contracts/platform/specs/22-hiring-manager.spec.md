# 22 — Hiring: Manager Surface

**Responsibility:** the manager's hiring operation — positions, assessment composition, inviting candidates, the pipeline, the candidate report and PDF, decisions, and the HR handoff. It does not own drill content ([12](12-drill-lifecycle.spec.md)), the candidate's own journey ([23](23-candidate-flow.spec.md)), token mechanics ([10](10-identity-and-access.spec.md)), or grading and review internals ([14](14-scoring-and-review.spec.md)).

## Overview

A manager opens a position, composes its assessment from their team's drills, and invites candidates by email. Candidates take the assessment through their tokenized link; the AI grades each drill; the manager reads one report per candidate — their own private note beside the AI's judgment — and decides. Approved candidates go to HR as an email shortlist with report PDFs attached. Positions, like everything else, belong to the manager who owns them ([FR-IDA-009](10-identity-and-access.spec.md)).

## Functional Requirements

### Positions

### FR-HIR-001: Positions list `[Must]`
The manager's hiring home shall list their positions, each with status — needs authoring, active, or closed — its invited, pending-review, and approved-unsent counts, and a primary action adapting to the most urgent next step (author the assessment · review N pending · send N to HR · view candidates). Opening an active or closed position leads to its pipeline (FR-HIR-010); a closed position's pipeline is read-only.

### FR-HIR-002: Position summary cards `[Could]`
The hiring home shall aggregate across the manager's positions: open positions, candidates pending review, and approved candidates not yet forwarded — the latter two acting as shortcuts when nonzero.

### FR-HIR-003: Create position `[Must]`
When the manager creates a position, the system shall accept: its candidate-facing title, the number of openings, the completion-notification toggle (E-5, [00 §6](00-overview.spec.md)), and the **candidate-report policy** — send right after finishing · send only to rejected candidates (default) · don't share — governing E-3 delivery ([FR-CND-012](23-candidate-flow.spec.md)). The call language displays as Egyptian Arabic, fixed ([00 §3](00-overview.spec.md)).

### FR-HIR-004: Assessment composition `[Must]`
The manager shall compose the position's assessment as an ordered set of drills from their team's published catalog — any count, reordered freely, each slot a stage in the order candidates will face ([FR-CND-004](23-candidate-flow.spec.md)) — and shall author new drills inline through the standard flow ([12](12-drill-lifecycle.spec.md)) with the position's language inherited and no assignment step.

### FR-HIR-005: Activation and freeze `[Must]`
A position becomes active when an assessment with at least one drill is saved. While no invite has been sent, the assessment remains editable; when the first invite is sent, the drill set and stage order freeze — every candidate of the position faces the identical assessment ([CMP-003](01-nfr-and-compliance.spec.md)).

### Candidates in

### FR-HIR-006: Add candidates `[Must]`
The manager shall add candidates as entries of {name, email, phone, LinkedIn, source} plus an **internal note** — private to the manager, never included in anything a candidate can see, resurfacing on the candidate's report (FR-HIR-011). Multiple candidates are added in one batch; an entry can be removed while more than one remains.

### FR-HIR-007: Bulk add by file `[Should]`
The manager shall alternatively upload a candidate list from a downloadable template; parsed rows pre-fill the batch for review and editing — sending invites always happens from the reviewed batch, never directly from a file.

### FR-HIR-008: Invite and send `[Must]`
Before sending, the manager shall set the invite's link expiry (from presets, default 7 days) and may edit the invite email template; the assessment allows one attempt per drill. Sending issues each candidate a token ([FR-IDA-011](10-identity-and-access.spec.md)) and emails each their invite (E-2), and returns the manager to their positions.

### FR-HIR-009: Resend `[Must]`
When the manager resends a candidate's invite (with confirmation), the system re-sends the templated email with a fresh token per the token rules ([FR-IDA-012](10-identity-and-access.spec.md)).

### The pipeline

### FR-HIR-010: Pipeline `[Must]`
A position's candidates shall present in three tabs with live counts: **Pipeline** (everyone, with per-candidate status — invited · in progress · completed — and decision state), **Review queue** (completed candidates ranked by overall score, descending), and **Approved** (approved candidates with their contact details). Per-row actions follow state: review for pending candidates, view report for decided ones, resend (FR-HIR-009) for invited ones.

### The report and the decision

### FR-HIR-011: Candidate report `[Must]`
A candidate's report shall present: their identity and applied-for position; drills completed and total time (the sum of actual call durations); the **overall score — the unweighted mean of their per-drill overall scores** — tier-banded ([FR-SCR-005](14-scoring-and-review.spec.md)); the manager's internal note (when one was written, visually distinct from AI content); the AI's cross-drill takeaway paragraph; and one card per drill — call type, buyer persona, duration, score, any restart flag ([FR-SCR-014](14-scoring-and-review.spec.md)) — each opening that drill's full review read-only with weights visible ([FR-SCR-018](14-scoring-and-review.spec.md), [FR-SCR-017](14-scoring-and-review.spec.md)).

### FR-HIR-012: Report PDF `[Must]`
Each completed candidate's report shall render as a PDF mirroring the report's content **minus the manager's internal note and every concealed element** ([FR-SCR-017](14-scoring-and-review.spec.md)) — one concealment-safe document serving both the HR attachment (FR-HIR-014) and the candidate's own copy ([FR-CND-012](23-candidate-flow.spec.md)) — downloadable from the report, with an embedded preview `[Should]`.

### FR-HIR-013: Decisions `[Must]`
The manager shall decide each completed candidate: **approve** or **reject**; an undecided candidate stays pending (leaving the report is not a decision). Approve-and-next routes to the next pending candidate's report, or back to the review queue when none remain. A decision may be changed while the candidate has not been included in a sent shortlist; once sent to HR, the decision is frozen.

### The handoff

### FR-HIR-014: Shortlist to HR `[Must]`
The manager shall send approved candidates to HR as one email (E-4): recipients chosen from the manager's saved HR contacts plus free-entry addresses (validated for form), the saved list editable and persistent; per-candidate checkboxes to hold candidates back from this batch (held-back candidates stay approved for a later batch); an editable email body; and each included candidate's PDF (FR-HIR-012) attached. On send, included candidates count as forwarded and leave the approved-unsent count.

### FR-HIR-015: Close position `[Should]`
When the manager closes a position, the system shall expire all its outstanding invite tokens, stop all further candidate entry, and keep the position and every report readable in a read-only archive.

### FR-HIR-016: Incomplete candidates `[Must]`
A candidate whose assessment ended incomplete — expiry mid-assessment or a consumed drill ([FR-CND-010](23-candidate-flow.spec.md), [FR-CND-011](23-candidate-flow.spec.md)) — shall present as incomplete, with every completed drill graded and reported; the manager may decide on the partial evidence.

### FR-HIR-017: Completion notification `[Should]`
Where a position's notification toggle is on, when a candidate completes the assessment, the system shall email the manager (E-5).

### FR-HIR-018: Position transfer `[Must]`
When BlueLab Internal Operations transfers a position to another manager ([FR-IDA-010](10-identity-and-access.spec.md)), the position shall carry its assessment, candidates, reports, decisions, and internal notes to the new owner, who holds it as their own thereafter; the former owner loses all access to it. Saved HR-recipient lists belong to each manager and do not transfer.

## NFR & compliance references

Identical-assessment comparability: [CMP-003](01-nfr-and-compliance.spec.md). Candidate personal data across pipeline, reports, and PDFs: [CMP-001](01-nfr-and-compliance.spec.md). Concealment in every delivered artifact: [FR-SCR-017](14-scoring-and-review.spec.md). Position/candidate scoping: [FR-IDA-009](10-identity-and-access.spec.md) under [CMP-004](01-nfr-and-compliance.spec.md).

## Acceptance Criteria

### AC-HIR-001: Position to active
Given a manager creating a position and composing three drills in order
When they save the assessment
Then the position is active, its stages in the set order
And the assessment stays editable until the first invite is sent, and frozen from then on.

### AC-HIR-002: Batch invite
Given a batch of four candidates, one with an internal note
When the manager sends invites with a 7-day expiry
Then four invite emails go out with four distinct tokens
And the note is stored, visible to no candidate, awaiting the report.

### AC-HIR-003: Report adds up
Given a candidate who completed drills scoring 8.0, 6.0, and 7.0, one carrying a restart flag
When the manager opens their report
Then the overall reads 7.0 (the unweighted mean), the restart flag is visible on its drill
And each drill card opens its full review with weights.

### AC-HIR-004: The PDF conceals
Given a completed candidate's report PDF
When its content is inspected
Then it mirrors the report's scores, takeaway, and per-drill results
And contains no internal note, no rubric weights, no challenges, and no hidden motives.

### AC-HIR-005: Decision lifecycle
Given an approved candidate not yet sent to HR
When the manager changes the decision to reject
Then the change applies
And after the candidate is included in a sent shortlist, no decision control is available.

### AC-HIR-006: Holdback stays approved
Given three approved candidates and one held back at sending
When the shortlist email sends
Then HR receives two PDFs, the held-back candidate remains approved and unsent
And reappears in the next shortlist's list.

### AC-HIR-007: Close expires access
Given an active position with one invited (unstarted) candidate
When the manager closes the position
Then the candidate's link no longer admits them
And all existing reports remain readable in the archive.

## Provenance

- Positions list and adaptive actions: walkthrough B11. Create position: B12. Assessment composition: B13. Inline drill authoring: B14/B14b. Pipeline tabs and states: B15/B17. Add candidates, internal note, invite settings and template: B16. Report and decision: B18. Drill replay: B18b. Shortlist composer: B19.
- The assessment freeze at first invite, decision freeze at shortlist send, close-expires-tokens, the concealment-safe single PDF format, and deciding on incomplete evidence: product decisions recorded in this file.
