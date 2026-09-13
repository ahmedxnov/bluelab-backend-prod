"""Drill assignment (FR-TRM-011/012/013). One row per drill, full-replace semantics:
re-assignment updates, never duplicates. Assignment deliberately sends no email —
the inventory of five is closed (specs/00 §6).
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.modules.training.schemas import AssignmentRecipientView, AssignmentView
from bluelab.platform.errors import catalog
from bluelab.platform.errors.denial import ProblemError
from bluelab.platform.ids import new_id

_MANAGEABLE_DRILL = text(
    """
    select d.status
      from drill d
     where d.id = :drill_id
       and d.org_id = :org_id
       and d.team_id = :team_id
       and not d.self_authored
    """
)

_TEAM_REP_COUNT = text(
    """
    select count(*)
      from account a
     where a.id = any(cast(:recipient_ids as uuid[]))
       and a.org_id = :org_id
       and a.team_id = :team_id
       and a.role = 'rep'
    """
)

_UPSERT_ASSIGNMENT = text(
    """
    insert into assignment
           (id, org_id, team_id, drill_id, due_date, attempts_allowed, created_by)
    values (:assignment_id, :org_id, :team_id, :drill_id, :due_date,
            :attempts_allowed, :created_by)
    on conflict (drill_id) do update
          set due_date = excluded.due_date,
              attempts_allowed = excluded.attempts_allowed,
              updated_at = now()
    returning id, drill_id, due_date, attempts_allowed, updated_at
    """
)
"""The unique drill key serializes competing manager sessions.

The statement obtains the assignment row lock before recipients are replaced, so
the following delete/insert set cannot interleave with another assignment write.
Whichever complete PUT obtains that lock last is the persisted state, matching
the contract's last-write-wins rule.
"""

_DELETE_RECIPIENTS = text("delete from assignment_recipient where assignment_id = :assignment_id")

_INSERT_RECIPIENTS = text(
    """
    insert into assignment_recipient
           (assignment_id, org_id, team_id, rep_account_id, attempts_used, granted_at)
    select :assignment_id, :org_id, :team_id, recipient_id, 0, now()
      from unnest(cast(:recipient_ids as uuid[])) as recipients(recipient_id)
    on conflict (assignment_id, rep_account_id) do nothing
    """
)

_RECIPIENTS = text(
    """
    select ar.rep_account_id as account_id, ar.attempts_used, ar.granted_at
      from assignment_recipient ar
     where ar.assignment_id = :assignment_id
     order by ar.rep_account_id asc
    """
)


def _unique_recipient_ids(recipient_ids: list[UUID]) -> list[UUID]:
    """Collapse duplicate JSON array entries into the recipient set they denote.

    OpenAPI intentionally specifies an array rather than imposing `uniqueItems`.
    Treating a duplicate as two recipients would instead hit the composite
    primary key and turn otherwise valid request syntax into a database error.
    """
    return list(dict.fromkeys(recipient_ids))


async def put_assignment(
    session: AsyncSession,
    *,
    manager_account_id: UUID,
    org_id: UUID,
    team_id: UUID,
    drill_id: UUID,
    recipient_account_ids: list[UUID],
    due_date: date,
    attempts_allowed: int,
) -> AssignmentView:
    """Create or fully replace a published team drill's assignment.

    A replacement deletes the prior recipient set and inserts a new one with
    `attempts_used = 0`. This is the fresh allowance required by FR-TRM-012 and
    leaves T-1 as the only consumer of that counter: admissions before this
    transaction commits are superseded by the new grant, while admissions after
    it atomically consume the newly inserted row.

    Raises:
        ProblemError: `404 not-found` for an absent/cross-team drill or recipient,
            and `409 drill-not-startable` for a draft or archived drill.
    """
    recipient_ids = _unique_recipient_ids(recipient_account_ids)
    scope = {"org_id": org_id, "team_id": team_id}

    drill = (
        await session.execute(_MANAGEABLE_DRILL, {**scope, "drill_id": drill_id})
    ).one_or_none()
    if drill is None:
        raise ProblemError(catalog.NOT_FOUND)
    if drill.status != "published":
        raise ProblemError(catalog.DRILL_NOT_STARTABLE)

    recipient_count = (
        await session.execute(
            _TEAM_REP_COUNT, {**scope, "recipient_ids": recipient_ids}
        )
    ).scalar_one()
    if int(recipient_count) != len(recipient_ids):
        # A cross-team id must not become an account-existence oracle. The same
        # generic denial also covers an id that is simply absent.
        raise ProblemError(catalog.NOT_FOUND)

    assignment = (
        await session.execute(
            _UPSERT_ASSIGNMENT,
            {
                **scope,
                "assignment_id": new_id(),
                "drill_id": drill_id,
                "due_date": due_date,
                "attempts_allowed": attempts_allowed,
                "created_by": manager_account_id,
            },
        )
    ).one()

    # Full replacement, rather than a differential update, is intentional: even
    # a recipient retained across a reassignment receives a fresh allowance.
    await session.execute(_DELETE_RECIPIENTS, {"assignment_id": assignment.id})
    await session.execute(
        _INSERT_RECIPIENTS,
        {
            **scope,
            "assignment_id": assignment.id,
            "recipient_ids": recipient_ids,
        },
    )
    recipients = (await session.execute(_RECIPIENTS, {"assignment_id": assignment.id})).all()

    return AssignmentView(
        drill_id=assignment.drill_id,
        due_date=assignment.due_date,
        attempts_allowed=assignment.attempts_allowed,
        recipients=[
            AssignmentRecipientView(
                account_id=row.account_id,
                attempts_used=row.attempts_used,
                granted_at=row.granted_at,
            )
            for row in recipients
        ],
        updated_at=assignment.updated_at,
    )
