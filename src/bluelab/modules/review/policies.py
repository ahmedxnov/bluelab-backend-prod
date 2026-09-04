"""RLS policy-class declarations for the tables this module owns (ADR-0031 §4).

Declarative only. `tools/generate_rls_policies.py` emits the `CREATE POLICY` set
from these declarations and `tools/check_rls_drift.py` diffs the live database
against the regenerated set on every commit. No policy is written by hand.

All five tables are **P6, participant-record**, and the class has three branches
that differ per table in exactly one respect — what a candidate may touch:

    attempt              rep: own · manager: team rows where NOT self_authored ·
                         candidate: own rows, read AND the T-1 admission insert
    transcript_entry     rep: own · manager: as above · candidate: NOTHING
    scorecard            rep: own · manager: as above · candidate: NOTHING
    dimension_score      follows scorecard                candidate: NOTHING
    moment               follows scorecard                candidate: NOTHING

**The candidate's exclusion from four of the five is the load-bearing part.**
FR-SCR-018 and AC-CND-003 say candidates see no evaluation, ever. Implemented as
the *absence* of a policy rather than as a projection filter, so there is no row
for a serialisation bug to leak — the same reason `rubric_dimension` has no
candidate policy at all.

The manager's `not self_authored` condition is AC-TRP-004: a rep's private
practice is invisible to their manager, and `attempt.self_authored` rides the
drill's composite FK so the policy-bearing copy cannot drift from the drill.
"""

from __future__ import annotations

from bluelab.platform.db.policy_class import REGISTRY, PolicyClass, TablePolicy

ATTEMPT = REGISTRY.register(
    TablePolicy(
        table="attempt",
        policy_class=PolicyClass.P6_PARTICIPANT_RECORD,
        owner_column="rep_account_id",
        candidate_read=True,
        candidate_insert=True,
    )
)
"""The one P6 table a candidate touches — and it needs the **insert**, not just
the read: T-1 creates the attempt as part of admission, and admission is the
candidate's own act (data/02 §1)."""

TRANSCRIPT_ENTRY = REGISTRY.register(
    TablePolicy(
        table="transcript_entry",
        policy_class=PolicyClass.P6_PARTICIPANT_RECORD,
        owner_column=None,
        parent_table="attempt",
    )
)
"""No candidate policy. A candidate cannot read back what they said, which also
means an assessment cannot be reverse-engineered from its own transcript."""

SCORECARD = REGISTRY.register(
    TablePolicy(
        table="scorecard",
        policy_class=PolicyClass.P6_PARTICIPANT_RECORD,
        owner_column=None,
        parent_table="attempt",
    )
)

DIMENSION_SCORE = REGISTRY.register(
    TablePolicy(
        table="dimension_score",
        policy_class=PolicyClass.P6_PARTICIPANT_RECORD,
        owner_column=None,
        parent_table="scorecard",
    )
)

MOMENT = REGISTRY.register(
    TablePolicy(
        table="moment",
        policy_class=PolicyClass.P6_PARTICIPANT_RECORD,
        owner_column=None,
        parent_table="scorecard",
    )
)

DECLARED = (ATTEMPT, TRANSCRIPT_ENTRY, SCORECARD, DIMENSION_SCORE, MOMENT)
