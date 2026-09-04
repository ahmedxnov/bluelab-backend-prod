"""The two scope constructors that bypass the product's own boundaries.

`system` reaches every row in an org. `ops` carries no org at all and is bounded
only by the verb policies. They are the two most dangerous objects in the
codebase, and until now any module could build one — a request handler that
constructed a system scope would silently defeat every policy the generator
emits.

**They live here so the import-linter can forbid reaching them.** `pyproject.toml`
carries a contract barring `bluelab.modules`, `bluelab.notifications`, and
`bluelab.api` from importing this module. The legitimate callers are exactly
three:

    bluelab.platform.queue.context   a worker, scoped from its job row
    bluelab.calls                    T-2 / T-6 from the signed internal seam
    bluelab.api.deps                 resolving an authenticated ops session

That is a build-time guarantee rather than a review convention — the same
discipline ADR-0002's module boundaries get, applied to the thing that would do
the most damage.

`ScopeContext.account()` and `.candidate()` stay on the class: they need a
credential to build and carry the caller's own limits.
"""

from __future__ import annotations

from uuid import UUID

from bluelab.platform.db.scope import PrincipalKind, ScopeContext


def system_scope(*, org_id: UUID, team_id: UUID | None = None) -> ScopeContext:
    """A worker or an internal-seam write, scoped from the job row or the call.

    Org-bounded — the `system` policies all carry `org_id = app.org_id`, so this
    is broad *within* one tenant and blind across tenants. That is the property
    that makes the work plane safe without giving it a per-request principal
    (ADR-0005, ADR-0031 decision 1).

    Args:
        org_id: Required. A system scope with no org matches nothing, which
            would be a silently dead worker rather than a safe one.
        team_id: Where the job has one.
    """
    return ScopeContext(
        principal_kind=PrincipalKind.SYSTEM, org_id=org_id, team_id=team_id
    )


def ops_scope() -> ScopeContext:
    """BlueLab internal operations.

    **No customer scope tuple at all** — the widest privilege in the system, and
    deliberately the narrowest reach: ops matches only the P9 tables and the
    enumerated verbs on `org` and `account`. There is no ops policy on any
    transcript, scorecard, moment, or knowledge table, so staff cannot read
    customer content even by mistake (ADR-0010 §2, ADR-0031 class P9).

    Every use is audited by the caller into `ops_audit` with a mandatory reason.
    """
    return ScopeContext(principal_kind=PrincipalKind.OPS)
