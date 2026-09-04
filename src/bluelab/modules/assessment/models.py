"""This module owns **no tables**, deliberately.

The candidate journey (FR-CND-*) is behaviour over rows other modules own:

    the plan and stage progression   `assessment_stage`   → hiring (FR-HIR-004
                                                             composition is a
                                                             manager act)
    pre-flight results               `candidate.preflight` → hiring
    the terminal completion marker   `candidate.completed_at` → hiring
    token entry                      `candidate_token`    → identity
    each stage's attempt             `attempt`            → review

That is not an oversight in the data design — it is what "the candidate has no
account" means physically. A candidate is a row in someone else's pipeline, and
their journey is derived state (V-10), never stored.

So this module is services, schemas, and routes over the published interfaces of
identity, hiring, and review — with `progression.py` and `preflight.py` holding
the rules. `policies.py` registers nothing for the same reason.

The rules this module enforces without owning storage:

* **Stage order and the one-restart limit** are serialised inside T-1
  (FR-CND-004/010) — the only place a strongly-consistent write happens on the
  call path.
* **Candidates see no evaluation, ever** (FR-CND-007, FR-SCR-018). Enforced by
  the *absence* of candidate policies on `scorecard`, `dimension_score`,
  `moment`, and `transcript_entry`, and by no candidate policy existing on
  `rubric_dimension` at all — structural, not projection-dependent
  (AC-CND-003).
* **Completion is terminal.** No dashboard, no resume; `completed_at` admits
  only the completion state and terminates every token at once (FR-IDA-013).
"""

from __future__ import annotations
