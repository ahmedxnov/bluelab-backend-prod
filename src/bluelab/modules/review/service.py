"""Domain services — this module's behaviour, and its published interface to the
other modules. Cross-module callers enter here; they never touch `models.py`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.adapters.object_store import (
    AuthorizedObjectRead,
    InvalidObjectKey,
    ObjectRef,
    ObjectStore,
)
from bluelab.modules.review.projection import moment_view, pinned_playback, rubric_view
from bluelab.modules.review.schemas import (
    AttemptStatus,
    AttemptView,
    BuyerView,
    CallType,
    DrillSummary,
    RecordingStatus,
    ReviewContext,
    ReviewView,
    ScoreBand,
    TranscriptEntryView,
)
from bluelab.platform.errors import catalog
from bluelab.platform.errors.denial import ProblemError, not_found
from bluelab.platform.resilience import DependencyUnavailable
from bluelab.platform.telemetry import metrics

_ATTEMPT = text(
    """
    select a.id, a.status, a.restart, a.started_at, a.ended_at,
           a.duration_seconds, a.rep_account_id, a.candidate_id,
           d.id as drill_id, d.label as drill_label, d.call_type,
           exists(select 1 from scorecard s where s.attempt_id=a.id) as has_scorecard
    from attempt a
    join drill d on d.id=a.drill_id
    where a.id=:attempt and a.org_id=:org
      and (
        a.rep_account_id=:account
        or (:is_manager and a.team_id=:team and not a.self_authored)
      )
    """
)


async def attempt_view(
    session: AsyncSession,
    *,
    attempt_id: UUID,
    org_id: UUID,
    team_id: UUID,
    account_id: UUID,
    is_manager: bool,
) -> AttemptView:
    row = (
        await session.execute(
            _ATTEMPT,
            {
                "attempt": attempt_id,
                "org": org_id,
                "team": team_id,
                "account": account_id,
                "is_manager": is_manager,
            },
        )
    ).first()
    if row is None:
        raise not_found()
    return AttemptView(
        id=row.id,
        drill=DrillSummary(
            id=row.drill_id,
            label=row.drill_label,
            call_type=cast(CallType, row.call_type),
        ),
        status=cast(AttemptStatus, row.status),
        restart=bool(row.restart),
        started_at=row.started_at,
        ended_at=row.ended_at,
        duration_seconds=row.duration_seconds,
        review_ready=row.status == "graded" and bool(row.has_scorecard),
    )


_REVIEW = text(
    """
    with numbered as (
        select x.id,
               row_number() over (
                   partition by x.drill_id, coalesce(x.rep_account_id, x.candidate_id)
                   order by x.started_at, x.id
               ) as attempt_number
        from attempt x
        where x.org_id=:org
    )
    select a.id as attempt_id, a.org_id, a.team_id, a.status,
           a.duration_seconds, a.recording_status, a.recording_object_key,
           a.rep_account_id, a.candidate_id,
           d.id as drill_id, d.label as drill_label, d.call_type,
           d.team_id as drill_team_id, d.author_account_id, d.self_authored,
           d.scenario,
           s.id as scorecard_id, s.overall_score,
           case when s.overall_score is not null then fn_score_band(s.overall_score) end
               as overall_band,
           s.takeaway,
           coalesce(rep.display_name, candidate.name) as participant_name,
           numbered.attempt_number
    from attempt a
    join drill d on d.id=a.drill_id
    left join scorecard s on s.attempt_id=a.id
    left join account rep on rep.id=a.rep_account_id
    left join candidate on candidate.id=a.candidate_id
    left join numbered on numbered.id=a.id
    where a.id=:attempt and a.org_id=:org
      and (
        a.rep_account_id=:account
        or (:is_manager and a.team_id=:team and not a.self_authored)
      )
    """
)

_DIMENSIONS = text(
    """
    select ds.rubric_dimension_id as dimension_id, rd.name, rd.weight,
           ds.score, fn_score_band(ds.score) as band, ds.note
    from dimension_score ds
    join rubric_dimension rd on rd.id=ds.rubric_dimension_id
    where ds.scorecard_id=:scorecard
    order by rd.ord, rd.id
    """
)

_MOMENTS = text(
    """
    select id,at_ms,severity,rubric_dimension_id as dimension_id,
           transcript_seq,quote,try_instead,why_it_matters
    from moment
    where scorecard_id=:scorecard
    order by at_ms,id
    """
)

_TRANSCRIPT = text(
    """
    select seq,speaker,at_ms,text
    from transcript_entry
    where attempt_id=:attempt
    order by seq
    """
)


def _buyer(scenario: Mapping[str, Any]) -> BuyerView:
    persona = scenario.get("persona")
    if not isinstance(persona, dict):
        raise TypeError("frozen scenario has no buyer persona")
    return BuyerView.model_validate(persona)


async def review_view(
    session: AsyncSession,
    *,
    attempt_id: UUID,
    org_id: UUID,
    team_id: UUID,
    account_id: UUID,
    is_manager: bool,
    object_store_factory: Callable[[], ObjectStore],
    authorization_seconds: int,
) -> ReviewView:
    row = (
        (
            await session.execute(
                _REVIEW,
                {
                    "attempt": attempt_id,
                    "org": org_id,
                    "team": team_id,
                    "account": account_id,
                    "is_manager": is_manager,
                },
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise not_found()
    if row["status"] != "graded" or row["scorecard_id"] is None:
        raise ProblemError(catalog.REVIEW_NOT_READY)

    dimension_rows = (
        (await session.execute(_DIMENSIONS, {"scorecard": row["scorecard_id"]}))
        .mappings()
        .all()
    )
    moment_rows = (
        (await session.execute(_MOMENTS, {"scorecard": row["scorecard_id"]}))
        .mappings()
        .all()
    )
    transcript_rows = (
        (await session.execute(_TRANSCRIPT, {"attempt": attempt_id}))
        .mappings()
        .all()
    )
    own = row["rep_account_id"] == account_id
    reveal_weight = row["author_account_id"] == account_id or (
        is_manager
        and row["drill_team_id"] == team_id
        and not bool(row["self_authored"])
    )
    moments = [moment_view(dict(item)) for item in moment_rows]

    recording_status = cast(RecordingStatus, row["recording_status"])
    recording_url: str | None = None
    if recording_status == "available" and row["recording_object_key"] is None:
        recording_status = "unavailable"
    elif recording_status == "available":
        expected_ref = ObjectRef.recording(org_id=org_id, attempt_id=attempt_id)
        try:
            object_store = object_store_factory()
            stored_ref = ObjectRef(str(row["recording_object_key"]))
            if stored_ref != expected_ref:
                raise InvalidObjectKey("recording key does not match its attempt")
            authorization = AuthorizedObjectRead(
                object_ref=stored_ref,
                principal_id=account_id,
                authorized_until=datetime.now(UTC)
                + timedelta(seconds=authorization_seconds),
            )
            recording_url = (await object_store.presign_get(authorization)).url
        except (DependencyUnavailable, InvalidObjectKey):
            recording_status = "unavailable"
    metrics.record_playback_access(outcome=recording_status)

    context = ReviewContext(viewer="own")
    if not own:
        context = ReviewContext(
            viewer="replay",
            participant_name=str(row["participant_name"]),
            attempt_number=int(row["attempt_number"]),
        )
    return ReviewView(
        attempt_id=attempt_id,
        context=context,
        drill=DrillSummary(
            id=row["drill_id"],
            label=str(row["drill_label"]),
            call_type=cast(CallType, row["call_type"]),
        ),
        buyer=_buyer(row["scenario"]),
        duration_seconds=int(row["duration_seconds"]),
        overall=ScoreBand(
            score=float(row["overall_score"]), band=row["overall_band"]
        ),
        takeaway=row["takeaway"],
        rubric_breakdown=[
            rubric_view(dict(item), reveal_weight=reveal_weight)
            for item in dimension_rows
        ],
        moments=moments,
        transcript=[
            TranscriptEntryView(
                seq=int(item["seq"]),
                speaker=item["speaker"],
                at_ms=int(item["at_ms"]),
                text=str(item["text"]),
            )
            for item in transcript_rows
        ],
        playback=pinned_playback(
            moments=moments,
            recording_status=recording_status,
            recording_url=recording_url,
        ),
    )
