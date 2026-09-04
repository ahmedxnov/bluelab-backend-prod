"""RLS policy-class declarations for the tables this module owns (ADR-0031 §4).

Declarative only. `tools/generate_rls_policies.py` emits the `CREATE POLICY` set
from these declarations and `tools/check_rls_drift.py` diffs the live database
against the regenerated set on every commit. No policy is written by hand.

Mostly **P8, hiring-manager** — the owning manager's own pipeline. Three tables
additionally carry a **P7 candidate-journey** branch, because the candidate must
see the assessment they are taking:

    position           P8 + P7 (own position, projected — title and org, no more)
    assessment_stage   P8 + P7 (own position's stages — the plan)
    candidate          P8 + P7 (own row, projected)

"Projected" is doing real work in those three. The candidate reads the *row*;
which **columns** reach them is the API layer's projection — `internal_note` and
`decision` never do (FR-HIR-012, AC-CND-003). Rows here, columns there: the split
is ADR-0031's and is not re-litigated per table.

`hr_contact` is the one P8 table scoped to an **account** rather than a team:
saved recipients are personal to the manager and are explicitly **not
transferred** with a position (FR-HIR-018).
"""

from __future__ import annotations

from bluelab.platform.db.policy_class import REGISTRY, PolicyClass, TablePolicy

POSITION = REGISTRY.register(
    TablePolicy(
        table="position",
        policy_class=PolicyClass.P8_HIRING_MANAGER,
        candidate_read=True,
        candidate_link="id",
    )
)
"""The candidate's own position, projected. Ops holds the transfer verb."""

ASSESSMENT_STAGE = REGISTRY.register(
    TablePolicy(
        table="assessment_stage",
        policy_class=PolicyClass.P8_HIRING_MANAGER,
        candidate_read=True,
        candidate_link="position_id",
        principal_commands=("select", "insert", "update", "delete"),
    )
)
"""The plan. Read-only for the candidate — stage *order* is enforced in T-1, not
by what they can see."""

CANDIDATE = REGISTRY.register(
    TablePolicy(
        table="candidate",
        policy_class=PolicyClass.P8_HIRING_MANAGER,
        candidate_read=True,
        candidate_link="id",
        candidate_link_target="candidate",
    )
)
"""Own row only. Never another applicant's — one candidate learning who else
applied would be a straightforward privacy failure."""

CANDIDATE_REPORT = REGISTRY.register(
    TablePolicy(table="candidate_report", policy_class=PolicyClass.P8_HIRING_MANAGER)
)
"""**No candidate policy**, despite E-3 sometimes mailing a candidate their own
report. The email carries a rendered, concealment-safe artifact; the table holds
the manager's working record. Those are different things, and only the first is
theirs (FR-CND-012)."""

HR_CONTACT = REGISTRY.register(
    TablePolicy(
        table="hr_contact",
        policy_class=PolicyClass.P5_OWNER_PRIVATE,
        org_scoped=True,
        team_scoped=False,
        owner_column="owner_account_id",
        principal_commands=("select", "insert", "update", "delete"),
    )
)
"""Declared P5 rather than P8: the shape *is* owner-private. Personal to the
manager, never transferred with a position (FR-HIR-018)."""

SHORTLIST = REGISTRY.register(
    TablePolicy(table="shortlist", policy_class=PolicyClass.P8_HIRING_MANAGER)
)

SHORTLIST_CANDIDATE = REGISTRY.register(
    TablePolicy(table="shortlist_candidate", policy_class=PolicyClass.P8_HIRING_MANAGER)
)
"""Membership is the decision freeze (FR-HIR-013). No candidate policy: whether
they were shortlisted is the hiring org's information, not theirs."""

DECLARED = (
    POSITION,
    ASSESSMENT_STAGE,
    CANDIDATE,
    CANDIDATE_REPORT,
    HR_CONTACT,
    SHORTLIST,
    SHORTLIST_CANDIDATE,
)
