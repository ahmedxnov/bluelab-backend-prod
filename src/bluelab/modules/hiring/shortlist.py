"""T-9 — the decision, and the shortlist send to HR under the decision freeze.
Plus T-8, position close, which shares this module's lifecycle concerns.

Shortlist membership bars further decision writes (`409 decision-frozen`,
FR-HIR-013). HR is an email recipient, never a user of the system.

## The freeze is membership, not a flag

There is no `decision_frozen` column. A candidate is frozen exactly when a
`shortlist_candidate` row exists, and the decision write is conditional on that
row's absence. `trg_decision_freeze` is the belt behind the braces — two
independent mechanisms, because the failure mode is a hiring decision being
quietly revised after it was sent to HR.

A held-back candidate simply has no membership row and stays approved-unsent —
which is why V-11 can count them without a status to maintain.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.adapters.email import validate_address
from bluelab.notifications.templates import safe_template_text
from bluelab.platform.errors import catalog
from bluelab.platform.errors.denial import ProblemError
from bluelab.platform.ids import new_id
from bluelab.platform.queue.catalog import Lane
from bluelab.platform.queue.enqueue import enqueue

_DECIDE = text(
    """
    update candidate
       set decision = :decision, decided_at = now(), updated_at = now()
     where id = :candidate
       and not exists (select 1 from shortlist_candidate sc where sc.candidate_id = :candidate)
    """
)

_HAS_EVIDENCE = text(
    """
    select exists (
        select 1 from attempt a where a.candidate_id = :candidate and a.status = 'graded'
    )
    """
)

_INSERT_SHORTLIST = text(
    """
    insert into shortlist (id, org_id, team_id, position_id, sent_by, recipients, email_body)
    values (:id, :org_id, :team_id, :position, :sent_by, cast(:recipients as jsonb), :body)
    """
)

_ADD_MEMBERS = text(
    """
    insert into shortlist_candidate (shortlist_id, candidate_id, org_id, team_id)
    select :shortlist, id, org_id, team_id from candidate
     where id = any(cast(:included as uuid[])) and decision = 'approved'
    """
)

_INSERT_EMAIL = text(
    """
    insert into email_send (id, org_id, kind, dedupe_key, shortlist_id)
    values (:id, :org_id, 'E4_shortlist', :dedupe_key, :shortlist)
    on conflict (kind, dedupe_key) do nothing
    """
)

_CLOSE_POSITION = text(
    """
    update position set status = 'closed', closed_at = now(), updated_at = now()
     where id = :position and status = 'active'
    """
)

_REVOKE_TOKENS = text(
    """
    update candidate_token t set revoked_at = now()
      from candidate c
     where c.id = t.candidate_id and c.position_id = :position and t.revoked_at is null
    """
)


async def decide(session: AsyncSession, *, candidate_id: UUID, decision: str) -> None:
    """Record an approve/reject (FR-HIR-013).

    Raises:
        ProblemError: `409 candidate-not-decidable` when no graded attempt exists
            — AI output is decision *support*, and there is nothing to support a
            decision with yet. `409 decision-frozen` once shortlisted.
    """
    if decision not in ("approved", "rejected"):
        raise ValueError(f"decision must be approved or rejected, got {decision!r}")

    has_evidence = (await session.execute(_HAS_EVIDENCE, {"candidate": candidate_id})).scalar_one()
    if not has_evidence:
        raise ProblemError(catalog.CANDIDATE_NOT_DECIDABLE)

    updated = await session.execute(
        _DECIDE, {"candidate": candidate_id, "decision": decision}
    )
    if updated.rowcount == 0:  # type: ignore[attr-defined]  # SQLAlchemy types async execute() as Result[Any]; the UPDATE it returns is a CursorResult at runtime
        # Conditional on there being no membership row. Zero rows means the
        # report already went to HR.
        raise ProblemError(catalog.DECISION_FROZEN)


async def send_shortlist(
    session: AsyncSession,
    *,
    position_id: UUID,
    org_id: UUID,
    team_id: UUID,
    sent_by: UUID,
    candidate_ids: list[UUID],
    recipients: list[str],
    email_body: str,
) -> UUID:
    """Run T-9's send half inside the caller's transaction.

    One transaction freezes every included decision by membership.

    Args:
        recipients: The resolved recipient list, snapshotted **as sent** — the
            manager's saved contacts may change afterwards, and the record of who
            received a candidate's report must not.

    Raises:
        ProblemError: `409 shortlist-empty` if no included candidate is approved.

    Returns:
        The shortlist id.
    """
    import json

    normalized = sorted({validate_address(value).lower() for value in recipients})
    if not normalized:
        raise ValueError("at least one recipient is required")
    body = safe_template_text(email_body)
    # The position lock serializes concurrent sends. Candidate locks make a
    # decision racing this transaction see shortlist membership before it can
    # update, so the T-9 freeze cannot be bypassed by stale UI state.
    await session.execute(text("select id from position where id=:position for update"), {"position": position_id})
    rows = (await session.execute(text("""select c.id,c.name,cr.pdf_object_key
      from candidate c join candidate_report cr on cr.candidate_id=c.id
     where c.position_id=:position and c.id=any(cast(:included as uuid[]))
       and c.decision='approved' and cr.pdf_status='available'
     for update"""), {"position": position_id, "included": [str(item) for item in candidate_ids]})).mappings().all()
    if len(rows) != len(set(candidate_ids)):
        raise ProblemError(catalog.SHORTLIST_EMPTY)
    snapshot = [{"candidate_id": str(row["id"]), "name": str(row["name"]), "pdf_object_key": str(row["pdf_object_key"])} for row in rows]
    shortlist_id = new_id()
    await session.execute(
        _INSERT_SHORTLIST,
        {
            "id": shortlist_id,
            "org_id": org_id,
            "team_id": team_id,
            "position": position_id,
            "sent_by": sent_by,
            "recipients": json.dumps(normalized),
            "body": body,
        },
    )

    # Filtered to `decision = 'approved'` in SQL rather than trusted from the
    # request: a stale client could include a candidate whose decision changed
    # since the page rendered, and sending a rejected candidate's report to HR is
    # not a recoverable mistake.
    added = await session.execute(
        _ADD_MEMBERS,
        {"shortlist": shortlist_id, "included": [str(c) for c in candidate_ids]},
    )
    if added.rowcount == 0:  # type: ignore[attr-defined]  # SQLAlchemy types async execute() as Result[Any]; the UPDATE it returns is a CursorResult at runtime
        raise ProblemError(catalog.SHORTLIST_EMPTY)

    email_send_id = new_id()
    await session.execute(
        _INSERT_EMAIL,
        {
            "id": email_send_id,
            "org_id": org_id,
            "dedupe_key": str(shortlist_id),
            "shortlist": shortlist_id,
        },
    )
    await enqueue(
        session,
        Lane.DISPATCH_EMAIL,
        {"email_send_id": str(email_send_id)},
        org_id=org_id,
        team_id=team_id,
    )
    await session.execute(text("update shortlist set candidate_snapshot=cast(:snapshot as jsonb) where id=:id"), {"id": shortlist_id, "snapshot": json.dumps(snapshot)})
    return shortlist_id


async def close_position(session: AsyncSession, *, position_id: UUID) -> bool:
    """T-8 — close the position and revoke every outstanding invite.

    Returns:
        True if this call closed it; False on a replay of an already-closed
        position, which api/00 §6 specifies as an idempotent `200`.

    Archive readability is a *policy* consequence of `status = 'closed'`, not a
    data move — nothing is copied anywhere (data/02 §1 T-8).
    """
    closed = await session.execute(_CLOSE_POSITION, {"position": position_id})
    if closed.rowcount == 0:  # type: ignore[attr-defined]  # SQLAlchemy types async execute() as Result[Any]; the UPDATE it returns is a CursorResult at runtime
        return False

    # Every live token dies with the position (FR-HIR-015). A candidate who never
    # started cannot begin an assessment for a role that no longer exists.
    await session.execute(_REVOKE_TOKENS, {"position": position_id})
    return True
