"""RLS policy-class declarations for the tables this module owns (ADR-0031 §4).

Declarative only. `tools/generate_rls_policies.py` emits the `CREATE POLICY` set
from these declarations and `tools/check_rls_drift.py` diffs the live database
against the regenerated set on every commit. No policy is written by hand.

Both owned tables are **P5, owner-private** — and the absence of a manager policy
is the point, not an omission. Coaching, not surveillance (P-1) is enforced here
rather than in a UI filter: a leaderboard query cannot accidentally join to a
rep's private badges or coaching feed, because no policy would return the rows.

The ratings, tiers, rosters and leaderboards a manager *does* see are derived on
read from `attempt` and `scorecard` (V-1…V-8), which carry their own P6 policies.
"""

from __future__ import annotations

from bluelab.platform.db.policy_class import REGISTRY, PolicyClass, TablePolicy

BADGE = REGISTRY.register(
    TablePolicy(
        table="badge",
        policy_class=PolicyClass.P0_REFERENCE,
        org_scoped=False,
        team_scoped=False,
    )
)
"""The catalogue is platform reference; the *awards* are private (below)."""

COACH_FEEDBACK_ITEM = REGISTRY.register(
    TablePolicy(
        table="coach_feedback_item",
        policy_class=PolicyClass.P5_OWNER_PRIVATE,
        org_scoped=True,
        team_scoped=False,
        owner_column="rep_account_id",
        principal_commands=("select", "update"),
    )
)
"""The rep's own feed. A manager coaches through the review surface, not by
reading the rep's private prompts."""

BADGE_AWARD = REGISTRY.register(
    TablePolicy(
        table="badge_award",
        policy_class=PolicyClass.P5_OWNER_PRIVATE,
        org_scoped=True,
        team_scoped=False,
        owner_column="account_id",
        principal_commands=("select",),
    )
)
""""Only you see these" (FR-TRP-005), as a policy rather than a copy line."""

DECLARED = (BADGE, COACH_FEEDBACK_ITEM, BADGE_AWARD)
