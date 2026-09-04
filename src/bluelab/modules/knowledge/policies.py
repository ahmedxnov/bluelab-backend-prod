"""RLS policy-class declarations for the tables this module owns (ADR-0031 §4).

Declarative only. `tools/generate_rls_policies.py` emits the `CREATE POLICY` set
from these declarations and `tools/check_rls_drift.py` diffs the live database
against the regenerated set on every commit. No policy is written by hand.

Every knowledge table is **P3, team-manager**: knowledge is authored and published
by the owning manager and by nobody else.

There is deliberately **no rep or candidate policy anywhere in this module**, even
though participants do see product facts. They see them through a *drill's frozen
answer-key snapshot* (FR-KNW-007, FR-KNW-009), never through these tables — which
is what stops a fact edit today from changing how an attempt was graded last
month, and keeps the participant-facing reference free of draft content that has
not been published.
"""

from __future__ import annotations

from bluelab.platform.db.policy_class import REGISTRY, PolicyClass, TablePolicy

PRODUCT_DOCUMENT = REGISTRY.register(
    TablePolicy(table="product_document", policy_class=PolicyClass.P3_TEAM_MANAGER)
)

FACT_SET = REGISTRY.register(
    TablePolicy(
        table="fact_set",
        policy_class=PolicyClass.P3_TEAM_MANAGER,
        principal_commands=("select", "insert", "update", "delete"),
    )
)
"""Both the live set and the in-review draft. A draft is unpublished truth, so it
is manager-only for the same reason the concealed set is."""

PRODUCT_FACT = REGISTRY.register(
    TablePolicy(
        table="product_fact",
        policy_class=PolicyClass.P3_TEAM_MANAGER,
        principal_commands=("select", "insert", "update", "delete"),
    )
)
"""DELETE granted: T-4 removes the superseded live set at publish, and choosing
manual entry or a new upload while a draft exists replaces it wholesale — both
manager acts inside the transaction (data/01 §3)."""

DOCUMENT_UPLOAD = REGISTRY.register(
    TablePolicy(table="document_upload", policy_class=PolicyClass.P3_TEAM_MANAGER)
)
"""Carries `object_key`, a handle to an uploaded source file in the object store.
Manager-only, so an object key cannot be enumerated by anyone else."""

DECLARED = (PRODUCT_DOCUMENT, FACT_SET, PRODUCT_FACT, DOCUMENT_UPLOAD)
