"""T-6 — interruption: void the attempt, reverse the allowance, release the lease.

A failed attempt is void and free (FR-LIV-014 / FR-LIV-015). An instance loss ends
its calls and the specification already prices that outcome — call state is
deliberately not replicated (ADR-0003).

## "Free" is the part that needs code

Voiding is easy — a status. *Free* means the allowance the admission CAS consumed
comes back, or a rep loses an attempt to a dropped connection. That reversal is
rep-only: a candidate's restart accounting is stage-based (FR-CND-010), and
returning an allowance they never had would silently grant a second restart.

## Nothing is captured

No transcript, no recording (data/03 §1). An interrupted attempt leaves the row
so the restart accounting can count it, and nothing else — which is also why the
transcript guard's insert check keys on the attempt still being `in_progress`.

`greatest(… - 1, 0)` rather than a bare decrement: the reconciliation sweep and
the runtime can both report the same interruption (ADR-0071 rule 7), and a
double-reversal would hand out an attempt nobody earned.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class Disposition(StrEnum):
    """Why the call ended, as classified by the runtime (api/01 §5).

    The runtime is the authority on this — only it has the facts — and it reports
    rather than executes (ADR-0071 rule 4).
    """

    PARTICIPANT_DROPPED = "participant_dropped"
    GRACE_EXCEEDED = "grace_exceeded"
    INSTANCE_LOST = "instance_lost"
    NEVER_ESTABLISHED = "never_established"


_VOID_ATTEMPT = text(
    """
    update attempt set status = 'interrupted', ended_at = now()
     where id = :attempt and status = 'in_progress'
    """
)

_REVERSE_ALLOWANCE = text(
    """
    update assignment_recipient ar
       set attempts_used = greatest(ar.attempts_used - 1, 0)
      from assignment a
     where a.id = ar.assignment_id
       and a.drill_id = :drill
       and ar.rep_account_id = :rep
    """
)

_DELETE_ATTEMPT = text("delete from attempt where id = :attempt and status = 'in_progress'")


async def interrupt(
    session: AsyncSession,
    *,
    attempt_id: UUID,
    drill_id: UUID,
    rep_account_id: UUID | None,
    disposition: Disposition,
) -> bool:
    """Run T-6 inside the caller's transaction.

    Args:
        rep_account_id: None for a candidate. The allowance reversal is rep-only.
        disposition: `NEVER_ESTABLISHED` **deletes** the attempt row rather than
            voiding it. FR-LIV-016 says a call that never established leaves
            nothing behind: an `interrupted` row would count against the
            candidate's restart allowance for a call that never happened.

    Returns:
        True if this call voided the attempt; False on a replay of an
        already-terminal one.
    """
    statement = _DELETE_ATTEMPT if disposition is Disposition.NEVER_ESTABLISHED else _VOID_ATTEMPT
    result = await session.execute(statement, {"attempt": attempt_id})
    if result.rowcount == 0:  # type: ignore[attr-defined]  # SQLAlchemy types async execute() as Result[Any]; the UPDATE it returns is a CursorResult at runtime
        return False

    if rep_account_id is not None:
        await session.execute(
            _REVERSE_ALLOWANCE, {"drill": drill_id, "rep": rep_account_id}
        )
    return True
