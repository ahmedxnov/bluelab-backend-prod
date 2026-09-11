# Grading evidence sourcing register

This register establishes the privacy-safe corpus and expert-panel intake used by the evaluator pilot and the full grading-validity study.

## Admission rules

- Corpus items are synthetic roleplays or recordings with explicit study consent and repository-use authorization.
- Production transcripts, recordings, candidate data, and customer content are ineligible.
- Every admitted item records provenance, consent authority, permitted uses, retention, withdrawal handling, call type, Egyptian Arabic register, and regional coverage.
- Reference labels remain a distribution of blind expert judgments. They are not represented as ground truth.
- Demeanor signals remain disabled until the registered fairness study passes.

## Coverage target

The full corpus contains 35–50 calls spanning discovery, post-proposal, renewal, and upsell. It spans formal and colloquial Egyptian Arabic and the regional variation required by the fairness instrument. At least three independent raters score each full-study call. Pilot membership is a preregistered subset of the same authorized corpus.

## Expert-panel criteria

Panelists demonstrate Egyptian Arabic fluency and current B2B insurance sales-coaching or sales-management expertise. Raters work independently, receive the same frozen rubric and answer-key snapshot, remain blind to model output and one another's scores, and declare conflicts before assignment.

## Intake register

| Source code | Source class | Authorization evidence | Coverage | State |
|---|---|---|---|---|
| SYN-001 | Scripted synthetic roleplay | Repository-authored fixture license | Discovery · colloquial Cairene | eligible for pilot construction |
| SYN-002 | Scripted synthetic roleplay | Repository-authored fixture license | Renewal · formal/colloquial mix | eligible for pilot construction |
| CONSENT-POOL | Consented participant roleplays | Signed study consent and use grant per item | Coverage matrix controls admission | intake channel defined; no item admitted |
| EXPERT-POOL | Independent expert raters | Conflict declaration and panel agreement per rater | Arabic fluency and insurance-sales expertise | screening criteria defined; no rater admitted |

## Evidence package

Each corpus release contains the immutable intake register, consent references, coverage matrix, transcript hashes, rubric version, blind rater assignments, and withdrawal ledger. Authorized source content remains in the evidence system recorded at intake. Repository fixtures use synthetic identifiers and contain no production data.
