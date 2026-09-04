"""T-2 — the completion hand-off (data/02 §1), executed here, not in the runtime.

One transaction: bulk transcript insert, then the status flip, then the
`grade_attempt` enqueue — committing together. That single-transaction property is
why the transcript is buffered in session memory for the whole call and crosses
the seam exactly once; per-segment streaming was rejected because it strands
orphan segments behind every interrupted call (ADR-0071 rule 3).

The `grade_attempt` insert **is** the completion event — the one true event in v1
(architecture/00 §4).

## The ordering is enforced, not merely intended

Transcript first, **then** the status flip. `trg_scorecard_freeze` refuses a
`transcript_entry` insert once the attempt has left `in_progress`, so getting
this backwards raises rather than silently dropping the capture. The freeze-guard
battery asserts that refusal directly
(`test_transcript_cannot_be_inserted_after_the_status_flip`).

## Why the status flip is conditional

`where id = :attempt and status = 'in_progress'` — zero rows means the attempt
was already completed or interrupted, and the caller is a **replay**. ADR-0071
rule 7 makes a lost completion request a real failure mode, so the reconciliation
sweep and the LiveKit webhook can both arrive after the runtime's own retry
succeeded. A replay must be a no-op, not a second grading.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.platform.queue.catalog import Lane
from bluelab.platform.queue.enqueue import enqueue


@dataclass(frozen=True, slots=True)
class TranscriptRow:
    """One captured utterance. `demeanor_label` is participant rows only."""

    seq: int
    speaker: str
    at_ms: int
    text: str
    demeanor_label: str | None = None


_INSERT_TRANSCRIPT = text(
    """
    insert into transcript_entry
        (attempt_id, seq, org_id, team_id, speaker, at_ms, text, demeanor_label)
    select :attempt, r.seq, :org_id, :team_id, r.speaker, r.at_ms, r.text, r.demeanor_label
    from jsonb_to_recordset(cast(:rows as jsonb))
        as r(seq int, speaker text, at_ms int, text text, demeanor_label text)
    """
)

_CLAIM_ATTEMPT = text(
    """
    select status from attempt where id = :attempt for update
    """
)

_COMPLETE_ATTEMPT = text(
    """
    update attempt
       set status = 'completed', ended_at = :ended_at, duration_seconds = :duration,
           recording_object_key = :recording_key, recording_status = :recording_status
     where id = :attempt and status = 'in_progress'
    """
)

_LAST_OPEN_STAGE = text(
    """
    update candidate set completed_at = now(), updated_at = now()
     where id = :candidate
       and completed_at is null
       and not exists (
           select 1 from assessment_stage st
           where st.position_id = (select position_id from candidate where id = :candidate)
             and not exists (
                 select 1 from attempt a
                 where a.candidate_id = :candidate
                   and a.assessment_stage_id = st.id
                   and a.status in ('completed','grading_pending','graded')
             )
       )
    """
)


async def complete(
    session: AsyncSession,
    *,
    attempt_id: UUID,
    org_id: UUID,
    team_id: UUID,
    rows: list[TranscriptRow],
    ended_at: datetime,
    duration_seconds: int,
    recording_object_key: str | None,
    candidate_id: UUID | None = None,
) -> bool:
    """Run T-2 inside the caller's transaction.

    Returns:
        True if this call completed the attempt; False if it was a replay of an
        already-terminal attempt and nothing was written.

    A replay returns False rather than raising: the runtime, the lease sweep, and
    the LiveKit webhook are three independent paths to the same completion
    (ADR-0071 rule 7), and only one of them can win. Losing is normal.
    """
    import json

    # CLAIM FIRST, then write. The obvious order — transcript, then a conditional
    # status flip — is wrong on the replay path: the transcript insert hits
    # `trg_scorecard_freeze` (the attempt has already left `in_progress`) and
    # RAISES, so a benign duplicate becomes a database error. The reconciliation
    # sweep and the LiveKit webhook both replay by design (ADR-0071 rule 7), so
    # that error would surface on a perfectly healthy call.
    #
    # `for update` also serialises two concurrent completions: the loser sees the
    # committed status and returns False instead of racing into the guard.
    claimed = (await session.execute(_CLAIM_ATTEMPT, {"attempt": attempt_id})).scalar_one_or_none()
    if claimed != "in_progress":
        return False

    if rows:
        # Bulk, one statement (FR-LIV-010) — and BEFORE the status flip, because
        # the transcript guard admits inserts only while the attempt is
        # in_progress. jsonb_to_recordset keeps it a single round trip regardless
        # of transcript length; a 15-minute call is hundreds of rows.
        await session.execute(
            _INSERT_TRANSCRIPT,
            {
                "attempt": attempt_id,
                "org_id": org_id,
                "team_id": team_id,
                "rows": json.dumps([
                    {
                        "seq": r.seq,
                        "speaker": r.speaker,
                        "at_ms": r.at_ms,
                        "text": r.text,
                        "demeanor_label": r.demeanor_label,
                    }
                    for r in rows
                ]),
            },
        )

    flipped = await session.execute(
        _COMPLETE_ATTEMPT,
        {
            "attempt": attempt_id,
            "ended_at": ended_at,
            "duration": duration_seconds,
            "recording_key": recording_object_key,
            # `pending` until the call plane confirms the upload. An upload that
            # never confirms is caught by the stale-pending sweep, which marks it
            # unavailable and raises the playback_asset fault — FR-SCR-013's
            # surfacing path, not a silently broken player.
            "recording_status": "pending" if recording_object_key else "none",
        },
    )
    if flipped.rowcount == 0:  # type: ignore[attr-defined]  # SQLAlchemy types async execute() as Result[Any]; the UPDATE it returns is a CursorResult at runtime
        return False

    if candidate_id is not None:
        # The terminal marker, set only when no stage remains open (FR-CND-008).
        # Setting it also terminates every token the candidate holds, because
        # token termination is derived from this column (FR-IDA-013).
        await session.execute(_LAST_OPEN_STAGE, {"candidate": candidate_id})

    # The completion event. This insert commits with the transcript and the
    # status flip or not at all — which is what makes "a completed call is never
    # voided or lost" a durability property rather than an aspiration.
    await enqueue(
        session,
        Lane.GRADE_ATTEMPT,
        {"attempt_id": str(attempt_id)},
        org_id=org_id,
        team_id=team_id,
    )
    return True
