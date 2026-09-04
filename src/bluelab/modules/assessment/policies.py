"""This module owns no tables, so it declares no policies.

Not an omission — see `models.py`. The candidate journey is behaviour over rows
that identity, hiring, and review own, because "the candidate has no account"
means a candidate is a row in someone else's pipeline.

The candidate's *access* is declared where the rows live:

    own candidate row, own position     → hiring   (class P7)
    the stage list                      → hiring   (class P7)
    own attempt rows + the T-1 insert   → review   (class P6, candidate branch)
    token validation                    → identity (system context only)

And the denials that matter are declared as **absences** in those same files: no
candidate policy on `scorecard`, `dimension_score`, `moment`,
`transcript_entry`, or `rubric_dimension`. Candidates see no evaluation, ever
(FR-SCR-018, AC-CND-003) — there is no row for a projection bug to leak.

`DECLARED` is empty and exported anyway, so the generator's module sweep finds a
symbol rather than an import error, and so a reader sees this as deliberate.
"""

from __future__ import annotations

from bluelab.platform.db.policy_class import TablePolicy

DECLARED: tuple[TablePolicy, ...] = ()
