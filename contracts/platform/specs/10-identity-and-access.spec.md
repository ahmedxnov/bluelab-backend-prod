# 10 — Identity & Access

**Responsibility:** how anyone enters the product and as what — provisioning, authentication, the role model, org membership, candidate access tokens, individual-account deactivation, and organization-level suspension.

## Overview

There is no self-service signup anywhere in BlueLab, and there are exactly **two entry paths — they never mix**:

**1. Account holders — managers and reps — sign in.** BlueLab Internal Operations creates their accounts from a roster the customer supplies, and the system emails them their initial credentials. At their **first sign-in**, one screen requires the password change, recording-consent checkbox, and terms acknowledgment before access. Organization service dates govern ordinary access; individual-account deactivation is a separate operator action.

**2. Candidates never sign in.** A candidate has no account, no password, and no first-sign-in step. They enter through a tokenized invite link scoped to one assessment for one position; their recording consent is captured at pre-flight instead, and their access ends when the assessment does.

## Functional Requirements

### FR-IDA-001: Operator provisioning `[Must]`
Each customer org shall carry one registered email domain. When BlueLab Internal Operations submits a customer roster entry of {email, role ∈ manager | rep, org — and, for a rep, their manager}, the system shall create an account with that email as its username, in that org, with that role, and (for reps) in that manager's team only when the normalized email domain exactly matches the org's registered domain. A mismatched domain shall be refused and the attempted provisioning shall be audited with its reason ([SEC-040](02-security-requirements.spec.md)).

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
While a token is within its expiry (set by the invite's settings) and its organization is within a current service term, the system shall admit it — including re-entry after leaving ([FR-CND](23-candidate-flow.spec.md)); when an invite is resent, the system shall issue a fresh token with a reset expiry, the prior token remaining valid until its own original expiry while the service term remains current. Organization suspension and service-date gating take precedence over token validity for previously issued and resent tokens, token exchanges, sessions created through tokens, and subsequent reads and writes. Ordinary token issuance and resend requests are refused before the term starts, after it ends, or while the organization is offboarding or purging.

### FR-IDA-013: Token termination `[Must]`
When a candidate completes their assessment, the system shall stop admitting their token to anything except the completion state; when a token passes its expiry unused or mid-assessment, the system shall refuse it with an expiry explanation.

### FR-IDA-014: Organization suspension `[Must]`
An authorized BlueLab operator shall confirm an organization's service start date, last access date, IANA timezone, contract reference, and applicable retention policy. The system shall display the resulting access and deletion dates before confirmation. Ordinary access begins at local 00:00 on the start date and ends at local 00:00 on the day after the last access date. Every ordinary admission and write boundary shall check those persisted UTC instants, independently of scheduled transition progress. At the service end, ordinary account-holder, candidate, integration, and worker access is suspended; existing sessions and credentials cannot extend it. An authorized early-termination decision suspends access immediately. The boundary shall fence in-flight writes so they cannot commit after the cutoff. Narrowly authorized offboarding administration, audit, purge preparation, and required subject-rights work remain available through distinct operations. Suspension preserves each account's independent activation state and manager assignment.

### FR-IDA-015: Operator lifecycle decisions `[Must]`
Only authorized BlueLab operators shall confirm or renew a service term, initiate early termination, cancel an unclaimed offboarding episode where permitted, approve an organization retention-policy revision, extend an episode's deadline, or create and release a deletion restriction. Each action shall identify its target organization, carry a reason and stable command identity, and be audited. A command targeting an existing episode shall identify that episode and its expected lifecycle sequence; a delayed retry shall return its recorded outcome rather than act on a later episode. Term expiry creates an episode from the service end instant, even when the transition job runs late. Renewal before expiry retains the original start date and extends the last access date without an access gap; renewal after expiry records a new start date and cancels the hold if purge has not been claimed. Renewal and claim shall have one serialized outcome. Renewal of a purging organization is refused.

Concurrent policy revision and term confirmation resolve to the policy effective when confirmation is durably accepted. If applicability or ordering cannot be established, confirmation is refused.

### FR-IDA-016: Access after renewal or cancellation `[Must]`
When an authorized operator renews during a hold or cancels an early-termination episode before purge claim, the organization lifecycle gate shall permit normal access only within the confirmed current service dates and under current account, token, and authorization rules. Independently deactivated accounts remain deactivated; revoked credentials and disconnected integrations are not silently restored; manager assignments remain unchanged. Operators shall see recovery actions still required for credentials and integrations. Cancellation does not revive an explicitly revoked candidate token.

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

### AC-IDA-008: Provisioning is domain-bound
Given an org whose registered domain is `example.com`
When an operator attempts to provision `rep@other.example` into that org
Then the account is not created
And the refused attempt is auditable with the actor, target org, email domain, and reason.

### AC-IDA-009: Organization suspension overrides valid credentials
Given an offboarding organization with an enabled manager account, an existing manager session, and an unexpired candidate token
When either principal requests ordinary organization data or tries to write through a new or existing session
Then access is refused by the organization lifecycle gate
And a previously issued or resent candidate token cannot exchange for access or start a call.

### AC-IDA-010: Renewal preserves independent access state
Given an offboarding organization with one enabled account and one independently deactivated account
When an authorized operator confirms a current renewal before purge claim
Then the enabled account may access the organization from the new start instant under normal authorization rules
And the deactivated account remains deactivated
And revoked credentials and disconnected integrations remain subject to explicit recovery.

### AC-IDA-011: Delayed lifecycle command is episode-bound
Given an initiation command whose outcome is recorded, followed by cancellation and a later episode
When the first command is retried or a cancellation targeting the first episode arrives late
Then neither command changes the later episode
And each retry returns its recorded outcome or a stale-target refusal.

### AC-IDA-012: Service end records the contractual deadline
Given an organization with August 15 as its confirmed last access date and no applicable contractual override
When local August 16 begins and the term transition is recorded
Then ordinary access is refused from local August 16 00:00 even when the transition job is late
And one episode records that service end instant, `org-default-90d:v1`, and a deadline 90 elapsed days later
And a repeated transition returns that episode without moving the deadline.

### AC-IDA-013: Contract policy ordering and freshness
Given an approved contractual revision racing with term confirmation
When both use the same expected organization sequence
Then one durable order determines the policy reference and stored deadline
And a stale confirmation retries policy resolution against the verified current head
And an unavailable or incomplete policy history prevents term confirmation.

### AC-IDA-014: Claim obeys hold and restrictions
Given a current offboarding episode with a future deadline or an active deletion restriction
When purge discovery or an episode job runs
Then no purge claim or destructive batch is authorized
And after the deadline with obligations resolved, claim records one run and ends cancellation eligibility.

### AC-IDA-015: Restriction during purge bounds deletion
Given a claimed purge with a bounded authorized batch
When an applicable restriction is accepted
Then the authorized batch may finish and is reported accurately
And no further destructive batch is authorized until the restriction is validly released
And the organization stays suspended.

### AC-IDA-016: Completion is independently evidenced
Given all live organization-owned rows, files, exports, derived state, and capabilities in the deletion inventory
When purge execution reaches finalization
Then every category passes its zero-remain or authorized-retention check
And independent completion evidence is durable before the run reports `completed`
And the organization row may be absent while its historical evidence remains readable to authorized operations.

### AC-IDA-017: Subject rights hold required source data
Given an accepted subject-rights request that needs source data during an offboarding hold or purge
When the request is accepted or awaits requester input
Then an applicable restriction serializes with claim and destructive authorization
And its 30-day response deadline does not automatically release the restriction
And a request after completion receives only the outcome supported by retained evidence.

### AC-IDA-018: Future term and renewal boundaries
Given a confirmed future start date and an explicit last access date in the organization's recorded zone
When the start date begins, the last access date ends, or a renewal is confirmed during the hold
Then access begins at the first boundary, remains available throughout the last access date, and ends at the following local midnight
And renewal before claim cancels the pending episode and grants access only from its new start instant
And a renewal racing with claim either commits first and blocks claim or is refused after claim.

### AC-IDA-019: Calendar-month retention
Given an August 15 last access date and an approved six-calendar-month retention period
When the hold begins at local August 16 00:00
Then purge becomes eligible at local February 16 00:00 after the approved six months
And an absent target day in a shorter month resolves to that month's last valid day.

### AC-IDA-020: Renewal before and during a hold
Given a service term starting February 15 with August 15 as its last access date
When the operator extends the term before expiry with September 15 as the last access date
Then access remains uninterrupted and the eventual hold begins at local September 16 00:00.
Given instead that the original term expires and the organization enters a hold on August 16
When the operator confirms a new term on September 17 before purge claim
Then the old episode is cancelled, the new term's start and end dates govern access, and its later
service end creates a fresh hold with a fresh retention countdown.

## Provenance

- Candidate link entry, pre-flight context: walkthrough C1; invite settings and resend: B16, B15.
- Provisioning, first-sign-in gate, individual-account deactivation, and organization suspension: product decisions recorded in this file.
