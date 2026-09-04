"""RLS policy-class declarations for the tables this module owns (ADR-0031 §4).

Declarative only. `tools/generate_rls_policies.py` emits the `CREATE POLICY` set
from these declarations and `tools/check_rls_drift.py` diffs the live database
against the regenerated set on every commit. No policy is written by hand.

Class tags come from the `-- policy:` comments in data/01 §1–§2. Where a table's
tag is a *combination* ("P9 read + system write"), the base class is declared here
and the qualifier is the flag the generator reads — see `system_write_only` and
`system_only` in `platform.db.policy_class`.
"""

from __future__ import annotations

from bluelab.platform.db.policy_class import REGISTRY, PolicyClass, TablePolicy

LEGAL_DOCUMENT_VERSION = REGISTRY.register(
    TablePolicy(
        table="legal_document_version",
        policy_class=PolicyClass.P0_REFERENCE,
        org_scoped=False,
        team_scoped=False,
    )
)
"""Readable by every principal, written only by migrations (data/01 §1)."""

ORG = REGISTRY.register(
    TablePolicy(
        table="org",
        policy_class=PolicyClass.P1_ORG,
        org_scoped=True,
        team_scoped=False,
        scope_self_column="id",
    )
)
"""The org row is its own scope: `id`, not `org_id`, is what the predicate binds."""

ACCOUNT = REGISTRY.register(
    TablePolicy(
        table="account",
        policy_class=PolicyClass.P2_ACCOUNT,
        org_scoped=True,
        team_scoped=True,
    )
)
"""A rep sees itself; a manager sees itself and its own team; ops holds the
provisioning verbs and no scope tuple (ADR-0031 §4)."""

CONSENT_RECORD = REGISTRY.register(
    TablePolicy(
        table="consent_record",
        policy_class=PolicyClass.P9_OPS,
        org_scoped=True,
        team_scoped=False,
        system_write_only=True,
    )
)
"""Ops may read (it is the CMP-002 evidence trail); only the system writes.

No account or candidate policy exists: a person cannot read, forge, or withdraw
their own consent row through the product surface. Withdrawal is an ops-mediated
subject-rights flow, not a self-service toggle.
"""

TERMS_ACCEPTANCE = REGISTRY.register(
    TablePolicy(
        table="terms_acceptance",
        policy_class=PolicyClass.P9_OPS,
        org_scoped=True,
        team_scoped=False,
        system_write_only=True,
    )
)
"""As `consent_record` — CMP-005's evidence trail, deliberately a separate table."""

PASSWORD_RESET_TOKEN = REGISTRY.register(
    TablePolicy(
        table="password_reset_token",
        policy_class=PolicyClass.P9_OPS,
        org_scoped=False,
        team_scoped=False,
        system_only=True,
    )
)
"""**System only — not even ops reads this.**

The row is a hashed, single-use credential. A table that no principal policy
grants is one an ops account cannot enumerate to mint a reset for someone, which
is the whole point (FR-IDA-006, ADR-0028). It carries no `org_id` because the
account reference already scopes it and adding one would invite a policy that
should not exist.
"""

CANDIDATE_TOKEN = REGISTRY.register(
    TablePolicy(
        table="candidate_token",
        policy_class=PolicyClass.P8_HIRING_MANAGER,
        org_scoped=True,
        team_scoped=True,
    )
)
"""The owning manager manages invites; the system validates on the candidate path.

**No candidate policy.** A candidate authenticates *with* a token and must never
be able to read the token table — that would let one candidate enumerate another
position's invites (architecture/02 §3.2).
"""

DECLARED = (
    LEGAL_DOCUMENT_VERSION,
    ORG,
    ACCOUNT,
    CONSENT_RECORD,
    TERMS_ACCEPTANCE,
    PASSWORD_RESET_TOKEN,
    CANDIDATE_TOKEN,
)
