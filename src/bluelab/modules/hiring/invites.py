"""T-7 — first invite send: assessment freeze plus token issue (data/02 §1).

`Idempotency-Key` is required on batch sends because the domain has no natural
request key; `email_send (kind, dedupe_key)` backstops at most-once per token.
A resend issues a *fresh* token and the prior token stays valid (FR-IDA-012).

## The freeze happens at most once

`coalesce(assessment_frozen_at, now())` rather than a bare assignment. Inviting a
second candidate must not move the freeze timestamp — that timestamp is the
evidence of *when* the assessment became fixed, and comparability rests on every
candidate having faced the same one (FR-HIR-005).

## Two idempotency mechanisms, at different layers

`Idempotency-Key` stops a double-submitted **batch**; `email_send (kind,
dedupe_key)` stops a double **send** to one recipient. The first is about the
request, the second about the effect — and only the second survives a client that
retries with a fresh key. A candidate receiving two invites with two live tokens
is a support call.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.platform.errors import catalog
from bluelab.platform.errors.denial import ProblemError
from bluelab.platform.ids import new_id
from bluelab.platform.queue.catalog import Lane
from bluelab.platform.queue.enqueue import enqueue
from bluelab.platform.security.tokens import hash_token, mint_token


@dataclass(frozen=True, slots=True)
class IssuedInvite:
    candidate_id: UUID
    token_id: UUID
    email_send_id: UUID
    """The `email_send` row this invite queued. Carried because it is the
    `dispatch_email` job's whole payload (api/02 §2) — discarding it and reaching
    for the token id instead sends the worker after a row that does not exist."""

    plaintext_token: str
    """Emailed once, never persisted. Travels in the invite link's URL fragment,
    so it reaches no server log (api/00 §3)."""


_FREEZE_ASSESSMENT = text(
    """
    update position set assessment_frozen_at = coalesce(assessment_frozen_at, now()),
                        updated_at = now()
     where id = :position and status <> 'closed'
    """
)

_HAS_STAGES = text(
    "select exists (select 1 from assessment_stage where position_id = :position)"
)

_INSERT_TOKEN = text(
    """
    insert into candidate_token (id, org_id, team_id, candidate_id, token_hash, expires_at)
    values (
        :id,
        :org_id,
        :team_id,
        :candidate,
        :token_hash,
        pg_catalog.now() + make_interval(days => :expiry_days)
    )
    """
)

_INSERT_EMAIL = text(
    """
    insert into email_send (id, org_id, kind, dedupe_key, candidate_id, token_id)
    values (:id, :org_id, 'E2_invite', :dedupe_key, :candidate, :token_id)
    on conflict (kind, dedupe_key) do nothing
    """
)

_EXPIRY_DAYS = text("select invite_expiry_days from position where id = :position")


async def send_invites(
    session: AsyncSession,
    *,
    position_id: UUID,
    org_id: UUID,
    team_id: UUID,
    candidate_ids: list[UUID],
) -> list[IssuedInvite]:
    """Run T-7 inside the caller's transaction.

    Raises:
        ProblemError: `409 assessment-empty` if the position has no stages, or
            `409 position-closed`.
    """
    has_stages = (await session.execute(_HAS_STAGES, {"position": position_id})).scalar_one()
    if not has_stages:
        # Inviting into an empty assessment would send a candidate a link to
        # nothing (FR-HIR-004).
        raise ProblemError(catalog.ASSESSMENT_EMPTY)

    frozen = await session.execute(_FREEZE_ASSESSMENT, {"position": position_id})
    if frozen.rowcount == 0:  # type: ignore[attr-defined]  # SQLAlchemy types async execute() as Result[Any]; the UPDATE it returns is a CursorResult at runtime
        raise ProblemError(catalog.POSITION_CLOSED)

    expiry_days = (await session.execute(_EXPIRY_DAYS, {"position": position_id})).scalar_one()
    issued: list[IssuedInvite] = []
    for candidate_id in candidate_ids:
        token_id = new_id()
        email_send_id = new_id()
        plaintext = mint_token()
        await session.execute(
            _INSERT_TOKEN,
            {
                "id": token_id,
                "org_id": org_id,
                "team_id": team_id,
                "candidate": candidate_id,
                "token_hash": hash_token(plaintext),
                "expiry_days": int(expiry_days),
            },
        )
        sent = await session.execute(
            _INSERT_EMAIL,
            {
                "id": email_send_id,
                "org_id": org_id,
                # Keyed on the TOKEN, not the candidate: a resend is a new token
                # and therefore legitimately a new send, while a retry of this
                # same request is not (api/02 §3).
                "dedupe_key": str(token_id),
                "candidate": candidate_id,
                "token_id": token_id,
            },
        )

        # One job per RECIPIENT, and only where a row was actually inserted.
        #
        # The batch is the unit of the request, never of the effect. A single job
        # for a batch of three leaves two candidates holding a token and a row
        # claiming an invite was queued, with no email ever sent — and in the
        # pipeline view that is indistinguishable from a candidate who ignored one
        # (FR-HIR-010).
        #
        # The `rowcount` guard is the other half: `on conflict do nothing` means
        # a duplicate (kind, dedupe_key) inserts nothing, and enqueuing for it
        # would dispatch against a row this transaction never created.
        if sent.rowcount:  # type: ignore[attr-defined]  # SQLAlchemy types async execute() as Result[Any]; the UPDATE it returns is a CursorResult at runtime
            await enqueue(
                session,
                Lane.DISPATCH_EMAIL,
                {"email_send_id": str(email_send_id)},
                org_id=org_id,
                team_id=team_id,
            )

        issued.append(
            IssuedInvite(
                candidate_id=candidate_id,
                token_id=token_id,
                email_send_id=email_send_id,
                plaintext_token=plaintext,
            )
        )

    return issued
