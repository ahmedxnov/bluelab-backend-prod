# 13 — Live Call

**Responsibility:** the pre-call brief and the live call itself, for every participant type (rep, candidate, author on a test call) — in-call surfaces, capture, how a call ends (participant, buyer, or the platform maximum), interruption handling, and the hand-off to grading. It does not own the drill content it renders ([12](12-drill-lifecycle.spec.md)), grading or any post-call artifact ([14](14-scoring-and-review.spec.md)), the journeys around the call ([20](20-training-rep.spec.md)/[22](22-hiring-manager.spec.md)/[23](23-candidate-flow.spec.md)), or consent capture points ([10](10-identity-and-access.spec.md), [CMP-002](01-nfr-and-compliance.spec.md)).

## Overview

Every call begins at a brief — the participant's last look at who they're about to talk to and why — and runs as a real-time voice conversation with the AI buyer, in Egyptian Arabic, with a live transcript. The call ends when the participant chooses to end it or the buyer brings the conversation to its natural close — with a platform maximum as a backstop — and hands the completed capture to grading. The same behavior serves practice, assessment, and authoring test calls; only persistence and destinations differ by participant type.

## Functional Requirements

### FR-LIV-001: Brief `[Must]`
While a participant may take a drill, the system shall present its brief before any call: the buyer's identity block (name, role, company, meta facts), the situation context, and a product summary with access to the full product reference ([FR-KNW-009](11-knowledge.spec.md)) — all rendered from the drill's frozen scenario ([FR-DRL-014](12-drill-lifecycle.spec.md)), with content appropriate to its call type (e.g. prior-proposal context for Post Proposal, account history for Renewal, sparse detail for Discovery/Cold Outreach).

### FR-LIV-002: Author's setup recap `[Should]`
While the brief's viewer is the drill's author, the system shall additionally show a collapsed, expandable recap of the drill's challenges and hidden motives, marked author-only; for any other viewer this recap shall not exist ([FR-SCR-017](14-scoring-and-review.spec.md)).

### FR-LIV-003: Calls start at briefs `[Must]`
The system shall start a call only from its drill's brief (or from the product reference reached via that brief); no path shall start a call without passing the brief.

### FR-LIV-004: Consent backstop `[Must]`
When a call is requested for a person with no recorded consent for the current notice version, the system shall not start the call ([CMP-002](01-nfr-and-compliance.spec.md)).

### FR-LIV-005: One live call `[Must]`
While a participant has an active live call, when a second call start is requested for that participant — through any session, device, or valid token of theirs — the system shall refuse with an explanation and leave the active call undisturbed.

### FR-LIV-006: In-call surfaces `[Must]`
During a live call, the system shall display: a live status indicator, the elapsed time, the buyer's identity with a one-line context recap (collapsible), and a speaking indicator distinguishing participant and buyer turns.

### FR-LIV-007: Live transcript `[Must]`
During a live call, the system shall render the conversation as a verbatim transcript in the call language, updating as turns complete, with each entry carrying its timestamp and speaker; a participant-controlled toggle shall hide the transcript (focus mode) and restore it, with capture continuing unaffected either way.

### FR-LIV-008: Mute `[Must]`
During a live call, the system shall provide a microphone mute toggle whose state is visible at all times.

### FR-LIV-009: Buyer conduct `[Must]`
During a live call, the system shall have the buyer converse in real time ([NFR-001](01-nfr-and-compliance.spec.md)) in Egyptian Arabic, in persona per the drill's frozen scenario — pursuing its hidden motives, pushing back per its challenges, and never breaking character or disclosing its concealed setup.

### FR-LIV-010: Capture `[Must]`
During a live call, the system shall capture the audio recording and the timestamped transcript; when the call ends, the transcript shall be complete — grading starts from it immediately ([NFR-005](01-nfr-and-compliance.spec.md)).

### FR-LIV-011: Ending a call `[Must]`
A call shall end as a normal completion when either the participant activates end-call — requiring confirmation that the call cannot be returned to — or the AI buyer brings the conversation to a natural close on the scenario reaching its conclusion (for example, committing to a next step or declining).

### FR-LIV-012: Maximum call length `[Must]`
The system shall enforce a platform maximum call length of 15 minutes as a safeguard against stalled or runaway calls: as a call nears the maximum the buyer shall move to conclude the conversation, and if the maximum is reached the system shall end the call automatically as a normal completion. A call's ordinary end is the participant ending it or the buyer's natural close (FR-LIV-011); the maximum is rarely reached.

### FR-LIV-013: Completion hand-off `[Must]`
When a call completes, the system shall submit its capture for grading ([FR-SCR-001](14-scoring-and-review.spec.md)) — except test calls ([FR-DRL-012](12-drill-lifecycle.spec.md)), which discard their capture — and route the participant to their destination: reps to the review path ([20](20-training-rep.spec.md)), candidates back to their assessment plan ([23](23-candidate-flow.spec.md)).

### FR-LIV-014: Interruption `[Must]`
When a live call is lost before completion — connection failure, closed browser, voice-path failure, or microphone loss beyond a brief reconnection grace — the system shall terminate the call, mark the attempt **interrupted**, and never grade it.

### FR-LIV-015: Interruption consequences `[Must]`
When a rep's or author's call is interrupted, the attempt shall be void with no effect on any attempts allowance, and the drill shall be startable again immediately; a candidate's interruption follows the candidate continuity policy ([FR-CND](23-candidate-flow.spec.md)).

### FR-LIV-016: Failure to establish `[Must]`
When a requested call fails to establish (the voice path cannot start), the system shall report the failure with a retry available, and no attempt shall be recorded or consumed by the failure.

## NFR & compliance references

Turn latency: [NFR-001](01-nfr-and-compliance.spec.md). Concurrency: [NFR-002](01-nfr-and-compliance.spec.md). Call availability: [NFR-003](01-nfr-and-compliance.spec.md). Transcript-complete-at-end feeds [NFR-005](01-nfr-and-compliance.spec.md). Consent: [CMP-002](01-nfr-and-compliance.spec.md). Browser/microphone support: [NFR-007](01-nfr-and-compliance.spec.md).

## Acceptance Criteria

### AC-LIV-001: Brief to call
Given a rep viewing an assigned drill's brief
When they review the product reference, return, and start the call
Then the live call opens with that drill's buyer in persona
And the brief's content matched the drill's frozen scenario.

### AC-LIV-002: Setup recap is author-only
Given a drill and two viewers of its brief — its author and another participant
When each opens the brief
Then the author sees the collapsed setup recap with challenges and hidden motives
And the other viewer's brief contains no trace of it.

### AC-LIV-003: Second call refused
Given a rep with a live call in progress
When a second call start is requested from another tab or device
Then it is refused with an explanation
And the first call continues unaffected.

### AC-LIV-004: Focus mode keeps capture
Given a live call with the transcript hidden in focus mode
When the call ends and grading completes
Then the full transcript exists, covering the hidden period completely.

### AC-LIV-005: Buyer concludes; maximum backstops
Given a live call whose scenario has reached its conclusion
When the buyer brings the conversation to a close
Then the call completes normally and is graded like any finished call
And separately, a call that reaches the 15-minute maximum ends automatically as a normal completion.

### AC-LIV-006: Rep interruption is free
Given a rep's call dropped by a network failure mid-drill
When they return to the drill
Then no review or grade exists for the interrupted attempt
And their attempts allowance is unchanged
And they can start the drill again immediately.

### AC-LIV-007: Consent backstop
Given a person with no consent record for the current notice version
When a call start is requested for them
Then the call does not start and the consent requirement is explained.

### AC-LIV-008: One call per participant across two valid links
Given a candidate holding two valid invite links (an original and a resent one) with a live call in progress started from one
When a call start is requested from the other link
Then it is refused with an explanation
And the live call continues unaffected.

## Provenance

- Brief layout and call-type variants: walkthroughs A1.5a/b/f/g, C1.5b; author recap: A1.5h. Live-call surfaces, focus mode, end-call confirm: A2, C2. Start-only-via-brief: A3.5 ("practice again" routes to the brief), A1.6/C1.6 ("start call" from the reference).
- Natural buyer-driven ending, the 15-minute platform maximum, interruption policies, the reconnection grace, and the one-live-call rule: product decisions recorded in this file.
