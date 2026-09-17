"""T-1 — call admission, for rep, candidate, and author (data/02 §1).

The gate every call passes, in one transaction: consent on record (FR-LIV-004,
CMP-002), token validity (FR-IDA-012/013), attempts allowance CAS (FR-TRP-013),
stage order (FR-CND-004), and acquisition of the single-live-call lease
(FR-LIV-005, keyed on the *participant* — ADR-0011).

Refusals are immediate and specified: `409 call-already-active`,
`409 allowance-exhausted`, `409 stage-not-next`, `409 stage-consumed`,
`503 call-capacity`. A call is never silently queued (FR-LIV-016).

Admission is user-facing latency — the participant is waiting — so
`admission.wall_ms` is a first-class metric and the R-17 migration trigger
(observability/01 §4.2).

## Order of operations, and why it is that order

The lease comes **first**, in Valkey, before the transaction opens. It is the
cheapest refusal and the one most likely to fire (a second tab), so paying for a
database round trip to discover it would be backwards. Every refusal after that
point releases the lease — hence the `try/except` shape rather than a linear
function.

Inside the transaction, correctness comes from **row locks and conditional
writes, not from elevated isolation** (data/02 §1). Everything runs at
`READ COMMITTED`:

* the allowance is an atomic CAS — `update … where attempts_used < attempts_allowed`
  updating zero rows *is* the refusal, so two racing tabs cannot both consume the
  last attempt;
* the candidate path takes `select … for update` on the candidate row first, so
  stage-order and restart accounting are serialised per candidate rather than
  globally.

## Test calls write nothing

`FR-DRL-012`: a test call takes the lease and runs **no transaction at all**. No
attempt row, no transcript, no capture — "leaves no trace in any statistic"
(AC-DRL-005) means exactly that, so the ephemerality is the absence of a write
rather than a cleanup step.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.platform.errors import catalog
from bluelab.platform.errors.denial import ProblemError
from bluelab.platform.ids import new_id


class ParticipantKind(StrEnum):
    REP = "rep"
    CANDIDATE = "candidate"
    AUTHOR = "author"


@dataclass(frozen=True, slots=True)
class AdmissionRequest:
    kind: ParticipantKind
    org_id: UUID
    team_id: UUID
    drill_id: UUID
    account_id: UUID | None = None
    candidate_id: UUID | None = None
    assessment_stage_id: UUID | None = None
    self_authored: bool = False


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    attempt_id: UUID
    restart: bool


_CONSENT_ON_RECORD = text(
    """
    select exists (
        select 1 from consent_record
        where account_id is not distinct from :account
          and candidate_id is not distinct from :candidate
          and notice_version = :current_version
    )
    """
)

_ALLOWANCE_STATE = text(
    """select ar.attempts_used,a.attempts_allowed from assignment_recipient ar
       join assignment a on a.id=ar.assignment_id
      where a.drill_id=:drill and ar.rep_account_id=:rep"""
)

_CONSUME_ALLOWANCE = text(
    """
    update assignment_recipient ar
       set attempts_used = ar.attempts_used + 1
      from assignment a
     where a.id = ar.assignment_id
       and a.drill_id = :drill
       and ar.rep_account_id = :rep
       and ar.attempts_used < a.attempts_allowed
    """
)

_LOCK_CANDIDATE = text("select id from candidate where id = :candidate for update")

_STAGE_STATE = text(
    """
    with plan as (
        select st.id, st.ord,
               exists (
                   select 1 from attempt a
                   where a.candidate_id = :candidate
                     and a.assessment_stage_id = st.id
                     and a.status in ('completed','grading_pending','graded')
               ) as done,
               (select count(*) from attempt a
                 where a.candidate_id = :candidate
                   and a.assessment_stage_id = st.id
                   and a.status = 'interrupted') as interrupted
        from assessment_stage st
        where st.position_id = (select position_id from candidate where id = :candidate)
    )
    select (select id from plan where not done and interrupted < 2 order by ord limit 1) as next_stage_id,
           (select ord from plan where not done and interrupted < 2 order by ord limit 1) as next_stage_ord,
           (select done from plan where id = :stage)                 as requested_done,
           (select interrupted from plan where id = :stage)          as requested_interrupted
    """
)

_INSERT_ATTEMPT = text(
    """
    insert into attempt (id, org_id, team_id, drill_id, rep_account_id, candidate_id,
                         assessment_stage_id, self_authored, restart, status)
    values (:id, :org_id, :team_id, :drill_id, :rep, :candidate,
            :stage, :self_authored, :restart, 'in_progress')
    """
)

_ORG_CALL_ACCESS = text(
    "select o.lifecycle_status, o.service_term_enforced, o.service_starts_at, "
    "o.service_ends_at, clock_timestamp() as checked_at, "
    "exists(select 1 from org_lifecycle_operation p where p.org_id=o.id "
    "and p.status='pending') as pending "
    "from org o where o.id=:org for share"
)


async def require_call_service_access(session: AsyncSession, org_id: UUID) -> None:
    """Fence the system-scoped internal call seam at the current service instant."""
    state = (await session.execute(_ORG_CALL_ACCESS, {"org": org_id})).one_or_none()
    if state is None or state.lifecycle_status != "active" or state.pending:
        raise ProblemError(catalog.ORGANIZATION_SUSPENDED)
    if state.service_term_enforced and (
        state.service_starts_at is None
        or state.service_ends_at is None
        or state.checked_at < state.service_starts_at
        or state.checked_at >= state.service_ends_at
    ):
        raise ProblemError(catalog.ORGANIZATION_SUSPENDED)


async def admit(
    session: AsyncSession, request: AdmissionRequest, *, consent_version: str
) -> AdmissionResult:
    """Run T-1. The lease must already be held by the caller.

    Raises:
        ProblemError: `409 consent-required`, `409 allowance-exhausted`,
            `409 stage-not-next`, or `409 stage-consumed`. Each maps to a
            designed UX state (ux/05 §3.4) — none is a generic failure.
    """
    await require_call_service_access(session, request.org_id)
    subject_kind = "candidate" if request.candidate_id is not None else "account"
    subject_id = request.candidate_id or request.account_id
    if subject_id is not None:
        await session.execute(
            text("select pg_advisory_xact_lock(hashtextextended(:subject,0))"),
            {"subject": str(subject_id)},
        )
        fenced = await session.scalar(
            text(
                "select exists(select 1 from erasure_request where org_id=:org "
                "and subject_kind=:kind and subject_id=:subject)"
            ),
            {
                "org": request.org_id,
                "kind": subject_kind,
                "subject": subject_id,
            },
        )
        if fenced:
            raise ProblemError(catalog.TOKEN_INVALID)

    if not await has_current_consent(
        session,
        account_id=request.account_id,
        candidate_id=request.candidate_id,
        consent_version=consent_version,
    ):
        # The universal backstop (AC-LIV-007): admission refuses even if a gate
        # upstream was somehow bypassed. Recording without consent is a CMP-002
        # breach, so it is checked at the last possible moment as well as the
        # first.
        raise ProblemError(catalog.CONSENT_REQUIRED)

    restart = False

    if request.kind is ParticipantKind.REP and not request.self_authored:
        # Self-authored practice has no assignment and therefore no allowance
        # (FR-TRP-009); an author testing their own drill is unmetered.
        consumed = await session.execute(
            _CONSUME_ALLOWANCE, {"drill": request.drill_id, "rep": request.account_id}
        )
        if consumed.rowcount == 0:  # type: ignore[attr-defined]  # SQLAlchemy types async execute() as Result[Any]; the UPDATE it returns is a CursorResult at runtime
            # Zero rows IS the refusal. Reading the counter and then writing it
            # would let two tabs both pass the read and both consume the last
            # attempt; the conditional update cannot.
            allowance = (
                await session.execute(
                    _ALLOWANCE_STATE,
                    {"drill": request.drill_id, "rep": request.account_id},
                )
            ).one_or_none()
            meta: dict[str, object] = (
                {
                    "attempts_used": int(allowance.attempts_used),
                    "attempts_allowed": int(allowance.attempts_allowed),
                }
                if allowance is not None
                else {}
            )
            raise ProblemError(catalog.ALLOWANCE_EXHAUSTED, meta=meta)

    if request.kind is ParticipantKind.CANDIDATE:
        restart = await _admit_candidate_stage(session, request)

    attempt_id = new_id()
    await session.execute(
        _INSERT_ATTEMPT,
        {
            "id": attempt_id,
            "org_id": request.org_id,
            "team_id": request.team_id,
            "drill_id": request.drill_id,
            "rep": request.account_id
            if request.kind is not ParticipantKind.CANDIDATE
            else None,
            "candidate": request.candidate_id,
            "stage": request.assessment_stage_id,
            "self_authored": request.self_authored,
            "restart": restart,
        },
    )
    return AdmissionResult(attempt_id=attempt_id, restart=restart)


async def has_current_consent(
    session: AsyncSession,
    *,
    account_id: UUID | None,
    candidate_id: UUID | None,
    consent_version: str,
) -> bool:
    """Check exactly one subject against the current recording notice."""
    if (account_id is None) == (candidate_id is None):
        return False
    return bool(
        (
            await session.execute(
                _CONSENT_ON_RECORD,
                {
                    "account": account_id,
                    "candidate": candidate_id,
                    "current_version": consent_version,
                },
            )
        ).scalar_one()
    )


async def _admit_candidate_stage(
    session: AsyncSession, request: AdmissionRequest
) -> bool:
    """Serialise per candidate, then check stage order and the restart allowance.

    The `for update` lock is what makes the rest safe: two tabs racing on the same
    stage both read "not yet done" without it, and both insert. Locking the
    candidate row rather than the stage means the whole plan is serialised for
    that one person and nobody else waits.

    Returns:
        Whether this attempt is the single permitted restart (FR-CND-010).
    """
    await session.execute(_LOCK_CANDIDATE, {"candidate": request.candidate_id})

    row = (
        await session.execute(
            _STAGE_STATE,
            {"candidate": request.candidate_id, "stage": request.assessment_stage_id},
        )
    ).one()

    if row.requested_done:
        raise ProblemError(catalog.STAGE_CONSUMED)

    interrupted = int(row.requested_interrupted or 0)
    if interrupted >= 2:
        # A twice-interrupted stage is consumed even when it was the final stage
        # and therefore no next_stage_id remains.
        raise ProblemError(catalog.STAGE_CONSUMED)

    if row.next_stage_id != request.assessment_stage_id:
        # One sitting, in order (FR-CND-004). `meta` drives the UX: the plan
        # re-renders with the correct stage pulsed once (ux/05 §3.4).
        raise ProblemError(
            catalog.STAGE_NOT_NEXT, meta={"next_stage_ord": row.next_stage_ord}
        )

    # 0 interrupted → fresh · 1 → the restart (data/02 §1 T-1).
    return interrupted == 1
