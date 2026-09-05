# 11 — Knowledge

**Responsibility:** the product-facts domain — product documents, their facts, the replace/review/publish flow, the grounding rule, and the content of the participant-facing product reference. It does not own how grading *uses* facts ([14](14-scoring-and-review.spec.md)), where the reference appears in a journey ([13](13-live-call.spec.md), [20](20-training-rep.spec.md), [23](23-candidate-flow.spec.md)), or scenario generation's consumption of facts ([12](12-drill-lifecycle.spec.md)).

## Overview

Knowledge is the **team's** answer key. Each team's manager maintains their team's product documents — teams typically own different product lines, so each team's facts are its own truth ([FR-IDA-009](10-identity-and-access.spec.md)). A team's published facts are the *only* product truth for that team: they ground each drill when it is built, and every drill freezes the facts it was built on — so grading and the reference a participant studies both check against that drill's own snapshot. One source per team, frozen per drill, no drift.

## Functional Requirements

### FR-KNW-001: Product documents `[Must]`
The system shall let each team's manager maintain up to **10 product documents** for their team, each holding a set of product facts; the v1 seed configuration ships a team with two (plan tiers & rates; claims & network).

### FR-KNW-002: Fact structure `[Must]`
Each product fact shall consist of a label, a value, and an optional note, belonging to exactly one product document.

### FR-KNW-003: Replace by upload `[Must]`
When a manager uploads a replacement source file (a supported document type, up to 20 MB) for a document, the system shall extract its facts and present them for review; oversize or unsupported files shall be refused at selection with the reason; nothing shall change in the live facts at upload time.

### FR-KNW-004: Replace by manual entry `[Should]`
When a manager chooses manual entry for a document, the system shall present an editable fact table prefilled with the document's current facts, supporting row edit, row removal, and row addition, leading to the same review step as upload.

### FR-KNW-005: Review with diff `[Must]`
While replacement facts await publishing, the system shall present them for review with every changed fact marked and its prior value visible, plus an explicit warning that publishing replaces the document's current facts.

### FR-KNW-006: Publish `[Must]`
When the manager confirms publishing, the system shall atomically replace the document's live facts with the reviewed set; there shall be exactly one live fact set per document at any time.

### FR-KNW-007: Grounding rule `[Must]`
A team's published facts shall be the sole source of product truth for that team's drills. At authoring, the current published facts ground scenario and rubric generation ([FR-DRL](12-drill-lifecycle.spec.md)); at publish the drill freezes the facts it was grounded in as its answer key ([FR-DRL-014](12-drill-lifecycle.spec.md)). Grading and the drill's product reference then use that frozen snapshot ([FR-SCR-002](14-scoring-and-review.spec.md), FR-KNW-009), so each drill is studied and graded against one unchanging truth. Updating a team's facts changes what newly published drills are built on, not existing drills. No team's consumers shall ever draw on another team's facts, and no other product-fact source shall exist.

### FR-KNW-008: Extraction failure `[Must]`
When extraction of an uploaded file fails or yields no usable facts, the system shall show the manager a clear failure with retry, and nothing shall reach the review step; the document's live facts remain untouched.

### FR-KNW-009: Product reference `[Must]`
The system shall render, as a read-only product reference reached from a drill's brief, the product facts that drill was grounded in (its frozen snapshot — so what a participant studies equals what grading checks), grouped by document, with a provenance line (source documents, publisher, fact count) and no edit affordance of any kind.

### FR-KNW-010: Draft status `[Should]`
While a document has a replacement in progress (uploaded or manually entered but not published), the system shall mark that document's status as draft-in-progress to managers, while the live facts continue serving all consumers.

### FR-KNW-011: Stale review guard `[Should]`
While a document's live facts have changed since a pending review was built (e.g. the same manager published from another concurrent session), when the manager confirms publishing from that stale review, the system shall reject the publish and rebuild the review against the current live facts for re-confirmation — so a confirmed diff is always a true statement about the facts it replaces.

## NFR & compliance references

Tenancy scoping: [CMP-004](01-nfr-and-compliance.spec.md); team scoping of documents and facts: [FR-IDA-009](10-identity-and-access.spec.md). Grounding underpins grading consistency ([NFR-004](01-nfr-and-compliance.spec.md)) — a stable answer key per team is a precondition of reproducible scores.

## Acceptance Criteria

### AC-KNW-001: Upload, review, publish
Given a document with live facts and a manager uploading a new rate card
When extraction completes, the manager reviews the diff (one value marked changed with the old value struck), and confirms publish
Then the document's live facts are the new set
And drills published after this are grounded in the new values, while already-published drills keep the facts they froze at publish.

### AC-KNW-002: Manual entry path
Given a manager choosing manual entry
When they edit one value in the prefilled table, remove a row, add a row, and proceed
Then the review shows exactly those three changes marked against the prior facts
And publishing applies precisely them.

### AC-KNW-003: Extraction failure
Given a manager uploading an unreadable file
When extraction fails
Then the manager sees the failure with a retry option
And the document's live facts are unchanged and still served everywhere.

### AC-KNW-004: Reference is read-only
Given a rep or candidate viewing the product reference
When they inspect every element
Then the briefed drill's grounded facts are present with the provenance line
And no fact of any other team appears
And no control exists that could alter any fact.

### AC-KNW-005: Concurrent publish guard
Given the same document under review in two concurrent sessions
When the second session confirms publish after the first already published
Then the second publish is rejected and a refreshed review is shown.

## Provenance

- Document library and cards: walkthrough D1. Facts / replace-upload / manual-entry / review-diff modes: D2 (all four modes). Participant reference and provenance line: A1.6, C1.6.
- The 10-document cap and multi-document generalization: product decision recorded in this file (the wireframes show the two-document seed).
- Grading and the participant reference use each drill's frozen fact snapshot (facts freeze into a drill at publish; updates reach new drills, or existing drills on republish): a deliberate refinement of the wireframes, which imply an always-current reference — chosen for full per-attempt comparability and hiring defensibility.
