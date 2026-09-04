"""RLS policy-class declarations for the tables this module owns (ADR-0031 §4).

Declarative only. `tools/generate_rls_policies.py` emits the `CREATE POLICY` set
from these declarations and `tools/check_rls_drift.py` diffs the live database
against the regenerated set on every commit. No policy is written by hand.

Every table here is **P9** — the ops plane's own storage. Ops holds the widest
privilege in the system and it is deliberately narrow: these five tables, plus
enumerated verbs on `org` and `account`, and nothing else.

**Ops appears in no policy on `transcript_entry`, `scorecard`, `dimension_score`,
`moment`, or any knowledge table.** That is ADR-0010 §2 made structural rather
than procedural: staff cannot read customer content even by mistake, and a fault
they are diagnosing carries ids and error classes instead of the transcript that
failed to grade.

`ops_account` and `ops_audit` carry no `org_id` — staff belong to BlueLab, not to
a customer org, and an audit row's `target_org_id` is a reference, not a scope.
"""

from __future__ import annotations

from bluelab.platform.db.policy_class import REGISTRY, PolicyClass, TablePolicy

OPS_ACCOUNT = REGISTRY.register(
    TablePolicy(
        table="ops_account",
        policy_class=PolicyClass.P9_OPS,
        org_scoped=False,
        team_scoped=False,
    )
)

OPS_AUDIT = REGISTRY.register(
    TablePolicy(
        table="ops_audit",
        policy_class=PolicyClass.P9_OPS,
        org_scoped=False,
        team_scoped=False,
        principal_commands=("select", "insert"),
    )
)
"""Append-only in behaviour. Nothing updates or deletes an audit row — an audit
trail that can be edited is not one (SEC-040)."""

OPS_FAULT = REGISTRY.register(
    TablePolicy(
        table="ops_fault",
        policy_class=PolicyClass.P9_OPS,
        org_scoped=True,
        team_scoped=False,
    )
)

ERASURE_REQUEST = REGISTRY.register(
    TablePolicy(
        table="erasure_request",
        policy_class=PolicyClass.P9_OPS,
        org_scoped=True,
        team_scoped=False,
    )
)

EXPORT_REQUEST = REGISTRY.register(
    TablePolicy(
        table="export_request",
        policy_class=PolicyClass.P9_OPS,
        org_scoped=True,
        team_scoped=False,
    )
)

DECLARED = (OPS_ACCOUNT, OPS_AUDIT, OPS_FAULT, ERASURE_REQUEST, EXPORT_REQUEST)
