# 10 — Identity & Access

**Responsibility:** how anyone enters the product and as what — provisioning, authentication, the role model, org membership, the candidate access token, and deactivation. It does not own what a role *sees* inside the product (visibility rules → [14](14-scoring-and-review.spec.md); journeys → [20](20-training-rep.spec.md)–[23](23-candidate-flow.spec.md)), nor the isolation guarantee itself ([CMP-004](01-nfr-and-compliance.spec.md)).

## Overview

There is no self-service signup anywhere in BlueLab, and there are exactly **two entry paths — they never mix**:

**1. Account holders — managers and reps — sign in.** BlueLab Internal Operations creates their accounts from a roster the customer supplies, and the system emails them their initial credentials. At their **first sign-in**, one screen requires both actions before anything else: set a new password, and select the recording-consent checkbox. After that, they sign in normally for life; offboarding is operator-mediated, like onboarding.

**2. Candidates never sign in.** A candidate has no account, no password, and no first-sign-in step. They enter through a tokenized invite link scoped to one assessment for one position; their recording consent is captured at pre-flight instead, and their access ends when the assessment does.

## Functional Requirements

### FR-IDA-001: Operator provisioning `[Must]`
When BlueLab Internal Operations submits a customer roster entry of {email, role ∈ manager | rep, org — and, for a rep, their manager}, the system shall create an account with that email as its username, in that org, with that role, and (for reps) in that manager's team.

### FR-IDA-002: No self-registration `[Must]`
The system shall provide no self-registration path for any role.

### FR-IDA-003: Credentials email `[Must]`
When an account is created, the system shall send the credentials email (E-1, [00 §6](00-overview.spec.md)) to the account's address with its initial credential.

### FR-IDA-004: First sign-in gate `[Must]`
While an account holds an initial credential, when its owner signs in, the system shall require — on one screen, before any further access — (a) setting a new password, (b) selecting the recording-consent checkbox per [CMP-002](01-nfr-and-compliance.spec.md), and (c) acknowledging the Terms of Use and Privacy Notice per [CMP-005](01-nfr-and-compliance.spec.md), the consent checkbox and the terms acknowledgment rendered as visibly distinct controls; completing any subset alone shall not grant access.

### FR-IDA-005: Authentication required `[Must]`
The system shall grant access to any manager or rep surface only to an authenticated account with the matching role; when authentication fails, the system shall refuse without disclosing whether the attempted account exists or its state.

### FR-IDA-006: Password reset `[Should]`
When an account owner requests a password reset, the system shall email a time-limited reset link (E-1) to the account's address; completing it shall set a new password and invalidate the link.

### FR-IDA-007: Role model `[Must]`
The system shall recognize exactly two customer account roles — Manager and Rep — and role shall determine which product surfaces an account can access ([20](20-training-rep.spec.md)/[21](21-training-manager.spec.md)/[22](22-hiring-manager.spec.md)).

### FR-IDA-008: Org membership `[Must]`
Every account shall belong to exactly one org, and every request it makes shall be scoped to that org ([CMP-004](01-nfr-and-compliance.spec.md)).

### FR-IDA-009: Manager-owned teams `[Must]`
Every rep shall belong to exactly one manager (their team); a manager may have many reps; an org may have many managers. **Team-scoped:** a manager's people-data — their reps' attempts, reviews, ratings, deep dives, and assignment — and their **drill catalog**: drills a manager authors are visible and usable within their team only (their own surfaces, their reps' library, their positions' assessments — [FR-DRL](12-drill-lifecycle.spec.md), [FR-TRM](21-training-manager.spec.md)); all statistics of a drill live within its owning team. **Hiring artifacts** — positions, assessments, candidates, decisions — belong to the manager who owns the position ([FR-HIR](22-hiring-manager.spec.md)). **Knowledge is team-scoped too:** each team maintains its own product documents and facts — its own answer key ([FR-KNW](11-knowledge.spec.md)). Nothing functional crosses the team boundary; the org is purely the tenancy shell ([CMP-004](01-nfr-and-compliance.spec.md)). The rep↔manager mapping is set at provisioning and changed only by BlueLab Internal Operations.

### FR-IDA-010: Deactivation `[Must]`
When BlueLab Internal Operations deactivates an account (on customer notice), the system shall refuse all further authentication for it while preserving its historical records (attempts, reviews, authored drills, decisions) unaltered. Before a manager is deactivated, their reps and their open positions shall be reassigned to another manager by BlueLab Internal Operations; the system shall block deactivating a manager who still owns reps or open positions.

### FR-IDA-011: Candidate access token `[Must]`
When a candidate is invited ([FR-HIR](22-hiring-manager.spec.md)), the system shall issue a unique access token, embedded in the invite link, granting access to exactly that candidate's assessment for that position — and nothing else.

### FR-IDA-012: Token validity `[Must]`
While a token is within its expiry (set by the invite's settings), the system shall admit it — including re-entry after leaving ([FR-CND](23-candidate-flow.spec.md)); when an invite is resent, the system shall issue a fresh token with a reset expiry, the prior token remaining valid until its own original expiry.

### FR-IDA-013: Token termination `[Must]`
When a candidate completes their assessment, the system shall stop admitting their token to anything except the completion state; when a token passes its expiry unused or mid-assessment, the system shall refuse it with an expiry explanation.

## NFR & compliance references

Isolation: every requirement above operates inside [CMP-004](01-nfr-and-compliance.spec.md). Consent capture: [CMP-002](01-nfr-and-compliance.spec.md) (FR-IDA-004 is its account-holder capture point). Personal-data rights over accounts and candidate records: [CMP-001](01-nfr-and-compliance.spec.md).

## Acceptance Criteria

### AC-IDA-001: First sign-in completes
Given a newly provisioned rep with the credentials email
When they sign in, set a new password, and select the consent checkbox
Then they land in the rep product surface
And their consent event is recorded with notice version.

### AC-IDA-002: Consent checkbox unselected
Given a newly provisioned rep on the first sign-in screen
When they set a new password but leave the consent checkbox unselected and submit
Then access is refused with the checkbox indicated as required
And no consent event is recorded.

### AC-IDA-003: Deactivated account
Given an account deactivated by BlueLab Internal Operations
When its owner attempts to sign in with correct credentials
Then authentication is refused with a deactivation notice
And the account's historical attempts remain visible in the org's team views.

### AC-IDA-004: Expired token
Given a candidate whose invite link expired yesterday
When they open the link
Then the assessment does not open and an expiry explanation is shown
And no drill can be started from that link.

### AC-IDA-005: Resent invite
Given a candidate with an unexpired original invite whose invite was just resent
When they open either the original or the new link before the original expiry
Then both admit them to the same assessment
And after the original expiry only the new link admits them.

### AC-IDA-006: Cross-org denial
Given a manager in org A
When they request any resource of org B by direct reference
Then the request is denied with no indication of the resource's existence.

### AC-IDA-007: Team boundary within an org
Given two managers A and B in the same org, each with their own reps
When manager A requests any of manager B's team data — a rep deep dive, a review, a drill, a drill's statistics, a knowledge document, or a position
Then the request is denied
And nothing of team B is reachable from team A in any surface.

## Provenance

- Candidate link entry, pre-flight context: walkthrough C1; invite settings and resend: B16, B15.
- Provisioning, first-sign-in gate, deactivation: no wireframe exists — product decisions recorded in this file.
