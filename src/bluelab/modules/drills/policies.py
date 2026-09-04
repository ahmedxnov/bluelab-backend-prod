"""RLS policy-class declarations for the tables this module owns (ADR-0031 §4).

Declarative only. `tools/generate_rls_policies.py` emits the `CREATE POLICY` set
from these declarations and `tools/check_rls_drift.py` diffs the live database
against the regenerated set on every commit. No policy is written by hand.

**`drill` is the one table whose class depends on the row, not the table.**
data/01 §4 tags it `P4 (published) / P3 (drafts) / P5 (self-authored)`, and all
three are true simultaneously across different rows:

    a team's published drill      P4  team reads, owning manager manages
    a team's draft                P3  manager only — unpublished is not shareable
    a rep's self-authored drill   P5  author only, INVISIBLE to the manager
                                      (AC-TRP-004)

So the declaration is P4 with `row_conditional=True`, and the generator emits the
three predicates as separate policies over one table. Splitting the table instead
was rejected upstream: a drill is one thing, and duplicating it would put the
freeze guard and the content hash in two places.

`rubric_dimension` and `drill_concealed` inherit that shape through their drill.
`drill_concealed` is the sharper case — it never gets a P4 branch at all, because
FR-SCR-017 lifts concealment by **authorship only**, so a team member reading a
published drill still cannot read its challenges and hidden motives.
"""

from __future__ import annotations

from bluelab.platform.db.policy_class import REGISTRY, PolicyClass, TablePolicy

AUTHORING_OPTION = REGISTRY.register(
    TablePolicy(
        table="authoring_option",
        policy_class=PolicyClass.P0_REFERENCE,
        org_scoped=False,
        team_scoped=False,
    )
)
"""The provided challenge / hidden-motive library. Custom entries are free text on
`drill_concealed`, not rows here — which is why the library is public reference
and the choices made from it are concealed."""

DRILL = REGISTRY.register(
    TablePolicy(
        table="drill",
        policy_class=PolicyClass.P4_TEAM_PUBLISHED,
        owner_column="author_account_id",
        row_conditional=True,
    )
)
"""Three row-conditional branches, plus two cross-team reads:

* the owning manager reads drills referenced by **their own positions' stages**,
  because after a transfer the assessment legitimately spans teams (FR-HIR-018)
  and its reviews must still render;
* a candidate reads the `drill` rows their own stages reference — **`drill` rows
  only**, never `rubric_dimension` (FR-SCR-018).
"""

DRILL_CONCEALED = REGISTRY.register(
    TablePolicy(
        table="drill_concealed",
        policy_class=PolicyClass.P3_TEAM_MANAGER,
        owner_column=None,
        authorship_gated=True,
    )
)
"""Manager of the owning team **or** the rep who authored it — and nobody else,
ever, in either product, even after the attempt ends (FR-SCR-017).

No P4 branch: publication does not make the concealed set readable. That is the
difference between this table and `drill`."""

RUBRIC_DIMENSION = REGISTRY.register(
    TablePolicy(
        table="rubric_dimension",
        policy_class=PolicyClass.P4_TEAM_PUBLISHED,
        owner_column="author_account_id",
        row_conditional=True,
        parent_table="drill",
    )
)
"""Follows its drill. **No candidate policy exists** — no candidate surface
renders rubric content, so denial is structural rather than
projection-dependent (FR-SCR-018, AC-CND-003).

The *weights* on this table are concealed from non-authors by the Review module's
column projection, not by RLS. Rows here, columns there — the split is ADR-0031's
and is not re-litigated per table."""

ASSIGNMENT = REGISTRY.register(
    TablePolicy(
        table="assignment",
        policy_class=PolicyClass.P3_TEAM_MANAGER,
        principal_commands=("select", "insert", "update", "delete"),
        rep_recipient_read=True,
    )
)
"""The manager manages it. A recipient rep **reads** the row they hold an
allowance on — nothing more.

An earlier version of this declaration said the rep sees their allowance and not
the assignment record at all. That could not hold: `assignment_recipient` carries
`attempts_used` but no `drill_id`, so a rep who could read only that table held a
counter with nothing to attach it to — and `due_date` and `attempts_allowed`, both
required on `LibraryCard.assignment`, live here. The rep's own note below already
assumed this read; it simply was not granted. FR-TRP-007's "Assigned to me" tab is
unreachable without it.

The grant is SELECT and is scoped by *membership*, not by team — a rep reads the
assignments they personally received, never a peer's. Who else holds the same
assignment stays invisible, which is P-1.

DELETE granted: archiving a drill cancels its assignment row outright
(FR-DRL-016), while every attempt and statistic referencing the drill survives."""

ASSIGNMENT_RECIPIENT = REGISTRY.register(
    TablePolicy(
        table="assignment_recipient",
        policy_class=PolicyClass.P3_TEAM_MANAGER,
        owner_column="rep_account_id",
        rep_own_read=True,
        principal_commands=("select", "insert", "update", "delete"),
    )
)
"""Manager manages; the rep reads their own row.

The rep needs it: `attempts_used` against `attempts_allowed` is what renders the
locked library card and the "{used} of {allowed} used" note (AC-TRP-005). They
must not read a teammate's — peer practice volume is exactly the surveillance
signal P-1 forbids."""

DECLARED = (
    AUTHORING_OPTION,
    DRILL,
    DRILL_CONCEALED,
    RUBRIC_DIMENSION,
    ASSIGNMENT,
    ASSIGNMENT_RECIPIENT,
)
