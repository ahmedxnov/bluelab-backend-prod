"""The RLS policy-class vocabulary — P0 reference, P1 org, P2 account,
P3 team-manager, P4 team-published, P5 owner-private, P6 participant-record,
P7 candidate-journey, P8 hiring-manager, P9 ops (ADR-0031 §4).

Each module declares the class of every table it owns in its own `policies.py`.
`tools/generate_rls_policies.py` reads those declarations and emits the whole
`CREATE POLICY` set; nobody writes a policy by hand, and
`tools/check_rls_drift.py` fails the build if the live database disagrees.

## Why a declaration and not a policy

Five principal kinds across roughly thirty-five tables is a hundred-and-seventy
policy decisions. Written by hand they drift, and **drift here is a breach**
(ADR-0031 context). Written from one declaration per table, the whole set is
regenerable and diffable, and the isolation suite attacks one mechanism instead
of thirty-five.

So a module says *what a table is*, and the generator decides *what that means*.
`REGISTRY` is the complete input to that generator, and its completeness is
itself checkable: every mapped table must appear, or the generator refuses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")
"""What a declaration may name.

The generator interpolates these strings straight into `CREATE POLICY` text —
there is no bind parameter for an identifier — and that text is then applied by
the MIGRATION role, which holds `BYPASSRLS`. So a malformed name here is not a
crash, it is arbitrary DDL executed at the highest privilege in the system.

Declarations are developer-authored, so this is not injection defence against a
user. It is the guard that stops a typo or a paste from becoming privileged SQL
that a reviewer skims past — and it belongs on the declaration rather than at each
interpolation site, because there were five identifier fields and only one of them
was being checked."""


class PolicyClass(StrEnum):
    """The ten classes of ADR-0031 §4.

    The column-level half of concealment is deliberately **not** here. RLS
    filters rows; the Review module's projection layer filters columns — rubric
    weights to non-authors, the candidate's own attempt projection, the manager's
    internal note (architecture/02 §6). The split is intentional and recorded, so
    a table's class never tries to express a column rule.
    """

    P0_REFERENCE = "P0"
    """Platform reference, no org: `badge`, `authoring_option`,
    `legal_document_version`. Readable by everyone, written by no one at runtime."""

    P1_ORG = "P1"
    """The org row itself. Own-org read; ops verb-scoped."""

    P2_ACCOUNT = "P2"
    """`account`. A rep sees itself; a manager sees itself and its own team;
    ops has provisioning verbs."""

    P3_TEAM_MANAGER = "P3"
    """Manager-only team data: knowledge tables, drafts, `drill_concealed`,
    `assignment`. **No rep policy exists at all** — this is where the concealed
    set lives, and FR-SCR-017 lifts concealment by authorship only."""

    P4_TEAM_PUBLISHED = "P4"
    """`drill` (published, not self-authored), `rubric_dimension`. Team read;
    owning manager full; plus the manager's read of drills referenced by their
    own positions' stages, because after a position transfer the assessment
    legitimately spans teams (FR-HIR-018).

    The candidate grant covers **`drill` rows only** — no candidate policy exists
    on `rubric_dimension`, so denial of rubric content is structural rather than
    projection-dependent (FR-SCR-018, AC-CND-003)."""

    P5_OWNER_PRIVATE = "P5"
    """Self-authored drills, `coach_feedback_item`, `badge_award`. Owner only —
    **invisible even to the manager** (AC-TRP-004)."""

    P6_PARTICIPANT_RECORD = "P6"
    """`attempt`, `transcript_entry`, `scorecard`, `dimension_score`, `moment`.
    Rep sees own; manager sees team rows where not self-authored; candidate gets
    own `attempt` rows plus the admission insert (T-1) and **no scorecard,
    transcript, or moment access at all**."""

    P7_CANDIDATE_JOURNEY = "P7"
    """The candidate's own row, position, and stages — projected."""

    P8_HIRING_MANAGER = "P8"
    """`position`, `candidate`, `candidate_report`, `shortlist*`,
    `candidate_token`, `email_send`, `hr_contact`."""

    P9_OPS = "P9"
    """`ops_*`, `erasure_request`, `export_request`. Ops full; system append."""


@dataclass(frozen=True, slots=True)
class TablePolicy:
    """One table's declaration — the generator's unit of input."""

    table: str
    policy_class: PolicyClass

    org_scoped: bool = True
    """Carries `org_id`. False only for P0 platform reference (data/00 §3)."""

    team_scoped: bool = True
    """Carries `team_id`. False for P0, P1, and the ops tables."""

    owner_column: str | None = None
    """For P5, the column holding the owning account. Required for that class."""

    scope_self_column: str = "org_id"
    """The column the org predicate binds to.

    `org` is the one table whose org scope is its own `id` rather than an `org_id`
    column. Everything else leaves this alone.
    """

    system_write_only: bool = False
    """The declared class grants **reads only**; writes are system-context.

    data/01 tags two tables `P9(read) + system(write)` — the consent and
    acceptance evidence trails. Ops must be able to *see* the CMP-002/005 record;
    nobody may forge one through a product surface.
    """

    system_only: bool = False
    """No principal policy at all — the system context is the only access.

    `password_reset_token` is the case: a hashed single-use credential that not
    even ops may enumerate. data/01 tags it "policy: system only".

    ADR-0031 §4's ten classes are a *summary* — the ADR states the generator's
    source is authoritative — and a table reachable by no principal is expressible
    as the absence of principal policies rather than as an eleventh class. Flagged
    in docs/open-items.md for confirmation.
    """

    parent_table: str | None = None
    """The table this one inherits visibility from.

    `transcript_entry` and `scorecard` hang off `attempt`; `dimension_score` and
    `moment` hang off `scorecard`; `rubric_dimension` off `drill`. None of them
    carry the columns the rule needs — `attempt.self_authored`, the participant
    identity — so their policies delegate to a helper that walks one hop.
    """

    row_conditional: bool = False
    """The class varies by **row state**, not by table.

    Only `drill` and `rubric_dimension`. data/01 §4 tags them
    `P4 (published) / P3 (drafts) / P5 (self-authored)`, and all three are true
    at once across different rows, so the generator emits three predicates over
    one table rather than one predicate per class.
    """

    authorship_gated: bool = False
    """Manager-of-owning-team **or** author, and nobody else — no team-read branch
    even once published.

    `drill_concealed` only. FR-SCR-017 lifts concealment by authorship alone, so
    publication must not make challenges and hidden motives readable. This flag is
    the difference between that table and `drill`.
    """

    rep_own_read: bool = False
    """A rep reads their own row on an otherwise manager-owned table.

    `assignment_recipient`: the rep needs `attempts_used` to render the locked
    card and the "{used} of {allowed}" note (AC-TRP-005), and must not see a
    teammate's — peer practice volume is the surveillance signal P-1 forbids.
    """

    rep_recipient_read: bool = False
    """A rep reads the PARENT row of an allowance they hold.

    `assignment` only, and it is the other half of `rep_own_read` above. That flag
    grants `attempts_used`; the rest of the card — `due_date` and
    `attempts_allowed` — lives here, on a table whose only other policies are
    manager and system. Without this, `rep_own_read`'s own stated purpose is
    unreachable: `assignment_recipient` carries no `drill_id`, so a rep holding an
    allowance row cannot discover which drill it is for, and FR-TRP-007's
    "Assigned to me" tab has nothing to list.

    Ownership is not a column here — it is the existence of the caller's
    `assignment_recipient` row — so this delegates to
    `app_assignment_granted_to_account` rather than comparing an `owner_column`.

    SELECT only. Allowance is consumed by the admission transaction (T-1) and
    granted by the manager (FR-TRM-012); a rep who could write this row could set
    their own due date.
    """

    candidate_read: bool = False
    """A candidate may read matching rows."""

    candidate_insert: bool = False
    """A candidate may insert. `attempt` only: T-1 creates the attempt as part of
    admission, and admission is the candidate's own act (data/02 §1)."""

    candidate_link: str | None = None
    """The column matched against the candidate's own scope.

    `id` on `candidate` and `position`; `position_id` on `assessment_stage`.
    """

    candidate_link_target: str = "position"
    """Which GUC `candidate_link` is compared against — `position` or `candidate`.

    Stated rather than inferred from the table and column names, which is how the
    first version guessed it. A guess that happens to be right is still a guess,
    and this one decides whether a candidate reads their own row or their whole
    position's.
    """

    principal_commands: tuple[str, ...] = ("select", "insert", "update")
    """Which commands the principal branches grant. **DELETE is not in the
    default, deliberately.**

    data/00 §2: real deletes or anonymising updates only, no soft-delete flags —
    and **erasure is the only remover** (ADR-0033). Archive, close and deactivate
    are *statuses*. So a table only gets DELETE where a requirement actually
    removes rows in normal operation:

        fact_set, product_fact   T-4 deletes the superseded live set at publish
        assignment*              archive cancels the assignment row (FR-DRL-016)
        assessment_stage         pre-freeze composition removes a stage
        hr_contact               a manager unsaves a recipient

    Everything else would be a rep deleting a bad score or an author deleting a
    published drill.
    """

    force_rls: bool = True
    """`FORCE ROW LEVEL SECURITY`. Always true for customer data (ADR-0031
    decision 3) — present as a field only so the generator can assert it rather
    than assume it."""

    IDENTIFIER_FIELDS = (
        "table",
        "owner_column",
        "scope_self_column",
        "parent_table",
        "candidate_link",
    )
    """Every field whose value reaches generated DDL as a bare SQL identifier.

    Enumerated rather than inferred from the type, because `str | None` also
    describes fields that are not identifiers — `candidate_link_target` is a
    keyword this class checks against a fixed pair, not a column name.
    """

    def __post_init__(self) -> None:
        for name in self.IDENTIFIER_FIELDS:
            value = getattr(self, name)
            if value is not None and not IDENTIFIER.match(value):
                raise ValueError(
                    f"{self.table}: {name}={value!r} is not a plain SQL identifier"
                )
        if self.policy_class is PolicyClass.P5_OWNER_PRIVATE and self.owner_column is None:
            raise ValueError(f"{self.table}: P5 requires owner_column")
        if self.policy_class is PolicyClass.P0_REFERENCE and (self.org_scoped or self.team_scoped):
            raise ValueError(f"{self.table}: P0 reference tables carry no scope columns")
        if self.team_scoped and not self.org_scoped:
            raise ValueError(f"{self.table}: team scope without org scope is not representable")
        if self.system_only and self.system_write_only:
            raise ValueError(f"{self.table}: system_only already implies no principal writes")
        if (
            (self.candidate_read or self.candidate_insert)
            and self.candidate_link is None
            and self.policy_class is not PolicyClass.P6_PARTICIPANT_RECORD
        ):
            raise ValueError(
                f"{self.table}: candidate access needs candidate_link "
                "(P6 resolves through the participant column instead)"
            )
        if self.candidate_insert and not self.candidate_read:
            raise ValueError(
                f"{self.table}: insert without read would let a candidate write a row "
                "they cannot then see — always a modelling mistake"
            )
        if self.row_conditional and self.owner_column is None:
            raise ValueError(
                f"{self.table}: row_conditional needs owner_column for its P5 branch"
            )
        if self.candidate_link_target not in ("position", "candidate"):
            raise ValueError(
                f"{self.table}: candidate_link_target must be 'position' or 'candidate'"
            )
        unknown = set(self.principal_commands) - {"select", "insert", "update", "delete"}
        if unknown:
            raise ValueError(f"{self.table}: unknown command(s) {sorted(unknown)}")
        if "select" not in self.principal_commands:
            raise ValueError(
                f"{self.table}: a principal that can write but not read is a modelling mistake"
            )


class PolicyRegistry:
    """Collects every module's declarations for the generator.

    Modules register at import time from their own `policies.py`. The generator
    imports all eight module packages, then reads this registry — so a table
    whose module forgot to declare it is absent, and the generator's completeness
    check against the SQLAlchemy metadata turns that into a build failure rather
    than a table quietly running without policies.
    """

    def __init__(self) -> None:
        self._entries: dict[str, TablePolicy] = {}

    def register(self, policy: TablePolicy) -> TablePolicy:
        """Declare one table's policy class.

        Raises:
            ValueError: If the table was already declared. Two modules claiming
                the same table is an ownership defect (ADR-0002), and silently
                letting the second win would hide it.
        """
        if policy.table in self._entries:
            raise ValueError(f"{policy.table}: declared twice — ownership conflict")
        self._entries[policy.table] = policy
        return policy

    def get(self, table: str) -> TablePolicy | None:
        return self._entries.get(table)

    def all(self) -> tuple[TablePolicy, ...]:
        return tuple(self._entries[name] for name in sorted(self._entries))

    def missing_from(self, table_names: frozenset[str]) -> frozenset[str]:
        """Tables that exist in the metadata but were never declared.

        The generator treats a non-empty result as fatal: an undeclared table is
        a table with no policies, and a table with no policies under
        `FORCE ROW LEVEL SECURITY` is either invisible or — if the force flag was
        also missed — wide open.
        """
        return frozenset(table_names) - frozenset(self._entries)


REGISTRY = PolicyRegistry()
"""The process-wide registry. Modules call `REGISTRY.register(...)`."""
