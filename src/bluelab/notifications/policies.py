"""RLS policy-class declaration for `email_send` (ADR-0031 class P8).

Declarative only — the generator reads this; nobody writes a policy by hand.

**P8, hiring-manager**, because the product-visible half of this table is the
hiring pipeline's invite-delivery state: a candidate who never received the invite
must be distinguishable from one who ignored it (FR-HIR-010). The manager who owns
the position is who that fact is for.

E-1 rows (credentials, password reset) are account-referenced and are not a
product surface at all — the system writes them and the manager predicate simply
does not match, since an E-1 row has no bearing on their team. That falls out of
the scope columns rather than needing a class of its own.

Org-scoped but not team-scoped: `email_send` carries `org_id` alone (data/01 §8),
so the manager predicate binds on org plus the referenced candidate's ownership
rather than on a `team_id` column that does not exist.
"""

from __future__ import annotations

from bluelab.platform.db.policy_class import REGISTRY, PolicyClass, TablePolicy

EMAIL_SEND = REGISTRY.register(
    TablePolicy(
        table="email_send",
        policy_class=PolicyClass.P8_HIRING_MANAGER,
        org_scoped=True,
        team_scoped=False,
    )
)

DECLARED = (EMAIL_SEND,)
