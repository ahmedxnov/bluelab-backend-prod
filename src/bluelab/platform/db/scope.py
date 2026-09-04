"""Transaction-local scope context (ADR-0031, decision 1).

Emits `SET LOCAL` for `app.principal_kind`, `app.org_id`, `app.team_id`,
`app.account_id`, `app.candidate_id`, `app.role` at transaction start. Missing
context reads as NULL and matches nothing: deny by default.

Workers resolve their context from the job row and set it the same way, so the
work plane has no unscoped path either (ADR-0005).

## Three implementation facts that are not obvious

**`set_config()`, not `SET LOCAL`.** `SET LOCAL app.org_id = :value` cannot take
a bind parameter — the grammar has no slot for one — so the only safe form is
`select set_config('app.org_id', :value, true)`, whose third argument *is*
`LOCAL`. Using literal interpolation to satisfy `SET LOCAL` would put a
request-controlled string into SQL text on the scope path, which is the one place
that must never happen.

**Absent values are set to the empty string, not skipped.** Every GUC is written
on every transaction, so the context is total rather than inherited. Policies
read `nullif(current_setting('app.org_id', true), '')::uuid`, which yields NULL
for unset *and* for empty — and a NULL predicate matches no rows. Skipping the
write would work too (the pooler resets `SET LOCAL` at commit) but it makes
correctness depend on the pooler's behaviour instead of on ours.

**Transaction-scoped, because the pooler is transaction-mode.** Nothing here may
outlive the transaction, at launch or at the target host (data/04 §8). That is
why this is applied per-transaction by `session.py` and never at connect time.

────────────────────────────────────────────────────────────────────────────────
ROUTE-BACK — `app.position_id` amends ADR-0031 decision 1. Needs owner sign-off.
────────────────────────────────────────────────────────────────────────────────

ADR-0031 lists six GUCs and omits the position. But architecture/02 §3.2 defines
the candidate token's binding as **`(org, position, candidate)`** — so the scope
tuple already includes it, and the GUC list was under-specified against the trust
model rather than deliberately narrower.

Without it, the three candidate-journey policies (`position`, `assessment_stage`,
`candidate`) each need a subquery back to `candidate` to find the position. Those
subqueries are the classic RLS footgun: a policy that reads an RLS-protected table
triggers that table's policies, which is at best a hidden cost on every candidate
read and at worst infinite recursion. The alternative is a `SECURITY DEFINER`
helper per table, which trades one enumerated escape hatch for three.

Carrying the position in the tuple that already conceptually contains it turns all
three into plain column comparisons. It also matches what
`platform.security.tokens.CandidateBinding` already returns.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_APPLY_SCOPE: Final = text(
    """
    select
      set_config('app.principal_kind', :principal_kind, true),
      set_config('app.org_id',         :org_id,         true),
      set_config('app.team_id',        :team_id,        true),
      set_config('app.account_id',     :account_id,     true),
      set_config('app.candidate_id',   :candidate_id,   true),
      set_config('app.position_id',    :position_id,    true),
      set_config('app.role',           :role,           true)
    """
)
"""All six GUCs in **one** statement.

Six separate `set_config` calls would be six round trips on every transaction —
including T-1 admission, where the participant is waiting, `admission.wall_ms` is
a first-class SLI, and the round trip to the data layer is precisely the seam
R-17 watches. A target list evaluates every call in one round trip with identical
semantics.
"""


class PrincipalKind(StrEnum):
    """The five principal kinds the policy matrix distinguishes (ADR-0031 §4)."""

    ACCOUNT = "account"
    CANDIDATE = "candidate"
    OPS = "ops"
    SYSTEM = "system"

    ANONYMOUS = "anonymous"
    """Authenticating — no principal resolved yet.

    **No policy names this kind, and that is the whole point.** Every generated
    predicate tests `app.principal_kind` against one of the four above, so an
    anonymous transaction matches nothing and reads zero rows from every
    customer-data table. It is deny-by-default expressed as a value rather than as
    an absence.

    It exists because sign-in has a genuine chicken-and-egg: the account lookup by
    email happens *before* there is a principal to scope to. Rather than reach for
    an unscoped session — which `session.py` refuses to expose at all — sign-in
    opens an ordinary scoped transaction that can see nothing, and asks one
    enumerated `SECURITY DEFINER` helper (`app_account_for_sign_in`) the single
    question it needs answered.
    """


class Role(StrEnum):
    """Account roles. Authorization is never role alone (architecture/02 §4)."""

    MANAGER = "manager"
    REP = "rep"


@dataclass(frozen=True, slots=True)
class ScopeContext:
    """The resolved principal, as the database will see it.

    Construct through the classmethods rather than the initialiser: each one
    encodes which fields a principal kind is *allowed* to carry, so an ops
    context cannot accidentally acquire an `org_id` and start reading customer
    rows (ADR-0010 — ops holds no customer scope tuple, verb-scoped access only).
    """

    principal_kind: PrincipalKind
    org_id: UUID | None = None
    team_id: UUID | None = None
    account_id: UUID | None = None
    candidate_id: UUID | None = None
    position_id: UUID | None = None
    role: Role | None = None

    @classmethod
    def account(
        cls,
        *,
        org_id: UUID,
        team_id: UUID,
        account_id: UUID,
        role: Role,
    ) -> ScopeContext:
        """A signed-in manager or rep.

        `team_id` is the owning manager's `account.id` — there is no separate
        team entity, because the specification defines none (data/00 §3). For a
        manager, `team_id == account_id`.
        """
        return cls(
            principal_kind=PrincipalKind.ACCOUNT,
            org_id=org_id,
            team_id=team_id,
            account_id=account_id,
            role=role,
        )

    @classmethod
    def candidate(
        cls, *, org_id: UUID, team_id: UUID, position_id: UUID, candidate_id: UUID
    ) -> ScopeContext:
        """A token-bearing candidate.

        Carries no `account_id` and no role: a candidate is not an account, and
        the P6/P7 policies grant on the candidate identity alone.

        Takes the whole `(org, position, candidate)` binding that
        `platform.security.tokens.verify_candidate_token` returns — never a bare
        candidate id. The token is validated *against the binding*, so the scope
        must carry the binding (architecture/02 §3.2).
        """
        return cls(
            principal_kind=PrincipalKind.CANDIDATE,
            org_id=org_id,
            team_id=team_id,
            position_id=position_id,
            candidate_id=candidate_id,
        )

    @classmethod
    def anonymous(cls) -> ScopeContext:
        """A transaction that reaches nothing — the sign-in path.

        Carries no ids at all, so every policy predicate that would compare one
        evaluates against NULL, and the kind itself matches no policy either.
        Both belts, deliberately: a future policy that forgot to test the kind
        would still find no id to match on.

        This is not a privileged scope and is safe for any module to build. It is
        the opposite of `privileged.system_scope` — that one reaches every row in
        an org, this one reaches none anywhere. What makes sign-in work under it
        is the one enumerated definer helper it then calls, not the scope.
        """
        return cls(principal_kind=PrincipalKind.ANONYMOUS)

    # `system` and `ops` are NOT constructible here. They live in
    # `platform.db.privileged`, behind an import-linter contract that bars
    # `bluelab.modules`, `bluelab.notifications`, and `bluelab.api` from reaching
    # them — a request handler that built a system scope would silently defeat
    # every policy the generator emits. The two constructors that remain both
    # require a credential to build and carry the caller's own limits.

    def as_gucs(self) -> dict[str, str]:
        """Render the context as the six GUC values, empty string for absent."""
        return {
            "principal_kind": self.principal_kind.value,
            "org_id": _render(self.org_id),
            "team_id": _render(self.team_id),
            "account_id": _render(self.account_id),
            "candidate_id": _render(self.candidate_id),
            "position_id": _render(self.position_id),
            "role": self.role.value if self.role is not None else "",
        }


async def apply_scope(session: AsyncSession, scope: ScopeContext) -> None:
    """Write the scope context into the current transaction.

    Must run inside an open transaction and before any statement that touches a
    customer-data table. `session.py` is the only intended caller — going around
    it means opening a transaction whose GUCs are unset, which denies everything
    rather than leaking anything, but is still a defect.

    One round trip, by construction — see `_APPLY_SCOPE`.
    """
    await session.execute(_APPLY_SCOPE, scope.as_gucs())


def _render(value: UUID | None) -> str:
    return str(value) if value is not None else ""
