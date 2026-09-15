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
| SYN-001…SYN-010 | Scripted synthetic roleplays | Per-item repository authorization, provenance, content hash, and transcript hash in `corpus.v1.json` | Four call types · Cairene, Alexandrian, Delta, Upper Egyptian, formal and colloquial registers | admitted to the fixed Phase 4 pilot corpus |
| CONSENT-POOL | Consented participant roleplays | Signed study consent and use grant per item | Coverage matrix controls admission | intake channel defined; no item admitted |
| EXPERT-POOL | Independent expert raters | Conflict declaration and panel agreement per rater | Arabic fluency and insurance-sales expertise | screening criteria defined; no rater admitted |

## Evidence package

Each corpus release contains the immutable intake register, consent references, coverage matrix, transcript hashes, rubric version, blind rater assignments, and withdrawal ledger. Authorized source content remains in the evidence system recorded at intake. Repository fixtures use synthetic identifiers and contain no production data.

## Executable evidence gate

`tools/collect_grading_study.py` runs every fixed fixture five times through the configured production evaluator, semantic validator, and server arithmetic. Each injection probe is paired at every run index with its hashed injection-stripped control through that same pipeline. The control changes only buyer instruction text and preserves participant evidence and timing. The collector rejects fixture mode and emits a content-free, corpus-bound run artifact. `tools/analyze_grading_study.py` applies the per-call-type one-sided Wilson lower bound, the paired injection tolerance, ICC(2,1), Bland–Altman analysis, human–human ceiling, deterministic bootstrap confidence intervals, and Bonferroni correction. A complete ten-call Phase 4 panel closes as `pilot_signal` when no threshold trips; a consistency or agreement breach closes as `escalate`. Full-power validation remains a Phase 10 disposition.

`tools/run_grading_canary.py` consumes an accepted reference panel and performs one live, validated re-grade per fixed fixture when no candidate artifact is supplied. It emits only fixture identifiers, call-type labels, numeric deltas, model-version telemetry, cost telemetry, observation time, and the closed breach result. `--model sentinel` selects the prescribed dated Haiku reference voter.

`tests/fixtures/grading/expert-evidence.template.v1.json` is an intentionally ineligible collection template. The gate accepts it only after the panel pre-registers the open thresholds and supplies complete, independently collected, blind, attested ratings from at least three qualified raters. Synthetic ratings and incomplete panels produce a non-passing report.
