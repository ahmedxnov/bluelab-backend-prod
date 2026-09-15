"""Manager report projections and the restricted report-render work item.

The two readers are intentionally separate.  ``manager_report`` may read the
manager-private note for J-22.  ``render_projection`` does not select that
column, ``drill_concealed``, or rubric tables at all, so the PDF cannot leak
them through a later template edit (FR-HIR-012 / SEC-016).
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.adapters.object_store import AuthorizedObjectRead, ObjectRef, ObjectStore
from bluelab.adapters.report_takeaway import CrossDrillBasis, DrillEvidence
from bluelab.platform.errors import catalog
from bluelab.platform.errors.denial import ProblemError, not_found
from bluelab.platform.ids import new_id
from bluelab.platform.queue.catalog import Lane
from bluelab.platform.queue.enqueue import enqueue


class ReportTakeawayProvider(Protocol):
    """The C-6 capability required by report rendering."""

    async def synthesize(self, basis: CrossDrillBasis) -> str: ...


@dataclass(frozen=True, slots=True)
class ReportCard:
    """A manager-visible completed stage and its existing grade."""

    stage_ord: int
    attempt_id: UUID
    drill_label: str
    call_type: str
    buyer_name: str
    duration_seconds: int
    score: float
    band: str
    restart: bool
    review_takeaway: str | None


@dataclass(frozen=True, slots=True)
class RenderProjection:
    """The only values allowed into the single deliverable PDF."""

    candidate_id: UUID
    org_id: UUID
    candidate_name: str
    position_title: str
    incomplete: bool
    drills_completed: int
    drills_total: int
    total_seconds: int | None
    overall_score: float | None
    overall_band: str | None
    cards: tuple[ReportCard, ...]
    stored_takeaway: str | None


_MANAGER_REPORT = text(
    """
    with report_base as (
        select c.id candidate_id,c.org_id,c.name,c.email,c.phone,c.linkedin,c.source,
               c.internal_note,c.decision,p.id position_id,p.title position_title,
               exists(select 1 from shortlist_candidate sc where sc.candidate_id=c.id) decision_frozen,
               coalesce(v.expired_incomplete,false) incomplete,
               cr.takeaway,coalesce(cr.pdf_status,'none') pdf_status
          from candidate c
          join position p on p.id=c.position_id and p.org_id=c.org_id and p.team_id=c.team_id
          left join v_candidate_state v on v.id=c.id
          left join candidate_report cr on cr.candidate_id=c.id and cr.org_id=c.org_id and cr.team_id=c.team_id
         where c.id=:candidate
    ), completed as (
        select a.candidate_id,count(*)::int drills_completed,
               sum(a.duration_seconds)::int total_seconds,
               avg(s.overall_score)::numeric(3,1) overall_score
          from attempt a join scorecard s on s.attempt_id=a.id
         where a.candidate_id=:candidate and a.status='graded'
         group by a.candidate_id
    ), totals as (
        select position_id,count(*)::int drills_total from assessment_stage
         where position_id=(select position_id from report_base) group by position_id
    )
    select r.*,coalesce(done.drills_completed,0) drills_completed,
           done.total_seconds,done.overall_score,
           case when done.overall_score is null then null else fn_score_band(done.overall_score) end overall_band,
           coalesce(t.drills_total,0) drills_total
      from report_base r
      left join completed done on done.candidate_id=r.candidate_id
      left join totals t on t.position_id=r.position_id
    """
)

_REPORT_CARDS = text(
    """
    select st.ord stage_ord,a.id attempt_id,d.label drill_label,d.call_type,
           a.duration_seconds,a.restart,s.overall_score,
           fn_score_band(s.overall_score) score_band,s.takeaway review_takeaway,
           d.scenario->'persona'->>'name' buyer_name
      from assessment_stage st
      join attempt a on a.assessment_stage_id=st.id and a.candidate_id=:candidate and a.status='graded'
      join scorecard s on s.attempt_id=a.id
      join drill d on d.id=a.drill_id and d.org_id=a.org_id
     where st.position_id=:position
     order by st.ord asc, a.id asc
    """
)

_RENDER_BASE = text(
    """
    with base as (
        select c.id candidate_id,c.org_id,c.name,p.id position_id,p.title position_title,
               coalesce(v.expired_incomplete,false) incomplete,cr.takeaway
          from candidate c
          join position p on p.id=c.position_id and p.org_id=c.org_id and p.team_id=c.team_id
          join candidate_report cr on cr.candidate_id=c.id and cr.org_id=c.org_id and cr.team_id=c.team_id
          left join v_candidate_state v on v.id=c.id
         where c.id=:candidate
    ), completed as (
        select a.candidate_id,count(*)::int drills_completed,
               sum(a.duration_seconds)::int total_seconds,
               avg(s.overall_score)::numeric(3,1) overall_score
          from attempt a join scorecard s on s.attempt_id=a.id
         where a.candidate_id=:candidate and a.status='graded'
         group by a.candidate_id
    ), totals as (
        select position_id,count(*)::int drills_total from assessment_stage
         where position_id=(select position_id from base) group by position_id
    )
    select b.*,coalesce(done.drills_completed,0) drills_completed,done.total_seconds,
           done.overall_score,case when done.overall_score is null then null else fn_score_band(done.overall_score) end overall_band,
           coalesce(t.drills_total,0) drills_total
      from base b
      left join completed done on done.candidate_id=b.candidate_id
      left join totals t on t.position_id=b.position_id
    """
)


def _card(row: Any) -> ReportCard:
    """Map a SQL row after explicitly checking nullable score/duration facts."""
    score = row["overall_score"]
    duration = row["duration_seconds"]
    if score is None or duration is None:
        raise ValueError("graded report card lacks score or duration")
    return ReportCard(
        stage_ord=int(row["stage_ord"]),
        attempt_id=UUID(str(row["attempt_id"])),
        drill_label=str(row["drill_label"]),
        call_type=str(row["call_type"]),
        buyer_name=str(row["buyer_name"] or "Buyer"),
        duration_seconds=int(duration),
        score=float(score),
        band=str(row["score_band"]),
        restart=bool(row["restart"]),
        review_takeaway=None if row["review_takeaway"] is None else str(row["review_takeaway"]),
    )


async def _cards(session: AsyncSession, *, candidate_id: UUID, position_id: UUID) -> tuple[ReportCard, ...]:
    rows = (await session.execute(_REPORT_CARDS, {"candidate": candidate_id, "position": position_id})).mappings().all()
    return tuple(_card(row) for row in rows)


async def manager_report(session: AsyncSession, *, candidate_id: UUID) -> dict[str, object]:
    """Return J-22's manager-only projection including the distinct private note."""
    row = (await session.execute(_MANAGER_REPORT, {"candidate": candidate_id})).mappings().one_or_none()
    if row is None:
        raise not_found()
    cards = await _cards(session, candidate_id=candidate_id, position_id=UUID(str(row["position_id"])))
    return {
        "candidate": {
            "id": row["candidate_id"], "name": row["name"], "email": row["email"],
            "phone": row["phone"], "linkedin": row["linkedin"], "source": row["source"],
        },
        "position": {"id": row["position_id"], "title": row["position_title"]},
        "incomplete": bool(row["incomplete"]),
        "decision": row["decision"],
        "decision_frozen": bool(row["decision_frozen"]),
        "drills_completed": int(row["drills_completed"]),
        "drills_total": int(row["drills_total"]),
        "total_seconds": None if row["total_seconds"] is None else int(row["total_seconds"]),
        "overall": None if row["overall_score"] is None else {"score": float(row["overall_score"]), "band": row["overall_band"]},
        "internal_note": row["internal_note"],
        "takeaway": row["takeaway"],
        "drill_cards": [
            {
                "stage_ord": card.stage_ord, "attempt_id": card.attempt_id, "drill_label": card.drill_label,
                "call_type": card.call_type, "buyer_name": card.buyer_name,
                "duration_seconds": card.duration_seconds, "score": {"score": card.score, "band": card.band},
                "restart": card.restart,
            }
            for card in cards
        ],
        "pdf_status": row["pdf_status"],
    }


async def render_projection(session: AsyncSession, *, candidate_id: UUID) -> RenderProjection | None:
    """Read the restricted construction projection, without private/concealed joins."""
    row = (await session.execute(_RENDER_BASE, {"candidate": candidate_id})).mappings().one_or_none()
    if row is None:
        return None
    cards = await _cards(session, candidate_id=candidate_id, position_id=UUID(str(row["position_id"])))
    return RenderProjection(
        candidate_id=UUID(str(row["candidate_id"])), org_id=UUID(str(row["org_id"])),
        candidate_name=str(row["name"]), position_title=str(row["position_title"]),
        incomplete=bool(row["incomplete"]), drills_completed=int(row["drills_completed"]),
        drills_total=int(row["drills_total"]),
        total_seconds=None if row["total_seconds"] is None else int(row["total_seconds"]),
        overall_score=None if row["overall_score"] is None else float(row["overall_score"]),
        overall_band=None if row["overall_band"] is None else str(row["overall_band"]),
        cards=cards, stored_takeaway=None if row["takeaway"] is None else str(row["takeaway"]),
    )


def takeaway_basis(projection: RenderProjection) -> CrossDrillBasis:
    """Use existing completed grades only; hidden input cannot reach the model."""
    return CrossDrillBasis(
        drills=[
            DrillEvidence(
                stage_ord=card.stage_ord, drill_label=card.drill_label, call_type=card.call_type,
                score=card.score, review_takeaway=card.review_takeaway,
            )
            for card in projection.cards
        ]
    )


def render_html(projection: RenderProjection, *, takeaway: str) -> str:
    """Construct inert HTML from the restricted projection with explicit RTL spans."""
    def safe(value: object) -> str:
        return html.escape(str(value), quote=True)

    score = "Not yet available" if projection.overall_score is None else f"{projection.overall_score:.1f} · {safe(projection.overall_band)}"
    rows = "".join(
        "<tr><td>{stage}</td><td>{label}</td><td>{buyer}</td><td>{duration}</td><td>{score}</td><td>{restart}</td></tr>".format(
            stage=card.stage_ord, label=safe(card.drill_label), buyer=safe(card.buyer_name),
            duration=f"{card.duration_seconds // 60}:{card.duration_seconds % 60:02d}",
            score=f"{card.score:.1f} · {safe(card.band)}", restart="Restart" if card.restart else "—",
        )
        for card in projection.cards
    )
    incomplete = "<p class=\"notice\">Assessment ended incomplete. Completed drills are shown.</p>" if projection.incomplete else ""
    return f"""<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; font-src data:; img-src data:\"><style>/*__BLUELAB_EMBEDDED_FONTS__*/@page{{size:A4;margin:16mm}}body{{font-family:BlueLabLatin,sans-serif;color:#19212b;font-size:11pt;line-height:1.45}}h1,h2{{font-family:BlueLabKufi,BlueLabLatin,sans-serif}}.score,.eyebrow{{font-weight:700}}.notice{{border-left:4px solid #b7791f;padding-left:10px}}table{{border-collapse:collapse;width:100%}}th,td{{border-bottom:1px solid #ccd3db;padding:7px;text-align:left;vertical-align:top}}.arabic{{font-family:BlueLabNaskh,serif;direction:rtl;unicode-bidi:plaintext}}</style></head><body><p class=\"eyebrow\">BLUELAB · CANDIDATE REPORT</p><h1>{safe(projection.candidate_name)}</h1><p>{safe(projection.position_title)} · {projection.drills_completed} of {projection.drills_total} drills · {safe(projection.total_seconds if projection.total_seconds is not None else '—')} seconds</p>{incomplete}<h2>Overall</h2><p class=\"score\">{score}</p><h2>Cross-drill takeaway</h2><p>{safe(takeaway)}</p><h2>Completed drills</h2><table><thead><tr><th>Stage</th><th>Drill</th><th>Buyer</th><th>Duration</th><th>Score</th><th>Restart</th></tr></thead><tbody>{rows}</tbody></table></body></html>"""


async def report_download(
    session: AsyncSession,
    *,
    candidate_id: UUID,
    principal_id: UUID,
    object_store: ObjectStore,
    authorization_seconds: int,
) -> tuple[str, datetime]:
    """Issue a one-object, short-lived report capability after scoped access."""
    row = (await session.execute(text("select org_id,pdf_object_key,pdf_status from candidate_report where candidate_id=:candidate"), {"candidate": candidate_id})).mappings().one_or_none()
    if row is None:
        raise not_found()
    if row["pdf_status"] != "available" or row["pdf_object_key"] is None:
        raise ProblemError(catalog.PDF_NOT_READY, meta={"pdf_status": row["pdf_status"]})
    expected = ObjectRef.report(org_id=UUID(str(row["org_id"])), candidate_id=candidate_id)
    if ObjectRef(str(row["pdf_object_key"])) != expected:
        raise ProblemError(catalog.PDF_NOT_READY, meta={"pdf_status": "failed"})
    issued = await object_store.presign_get(
        AuthorizedObjectRead(
            object_ref=expected,
            principal_id=principal_id,
            authorized_until=datetime.now(UTC) + timedelta(seconds=authorization_seconds),
        )
    )
    return issued.url, issued.expires_at


async def request_render(session: AsyncSession, *, candidate_id: UUID) -> tuple[str, bool]:
    """Claim a none/failed PDF slot; the caller enqueues only when it changed."""
    row = (await session.execute(text("""
        insert into candidate_report(candidate_id,org_id,team_id,pdf_status)
        select id,org_id,team_id,'none' from candidate where id=:candidate
        on conflict (candidate_id) do nothing
    """), {"candidate": candidate_id}))
    _ = row
    changed = (await session.execute(text("""
        update candidate_report set pdf_status='pending',pdf_object_key=null,pdf_rendered_at=null
         where candidate_id=:candidate and pdf_status in ('none','failed')
         returning pdf_status
    """), {"candidate": candidate_id})).scalar_one_or_none()
    if changed is not None:
        return "pending", True
    status = (await session.execute(text("select pdf_status from candidate_report where candidate_id=:candidate"), {"candidate": candidate_id})).scalar_one_or_none()
    if status is None:
        raise not_found()
    return str(status), False


async def store_rendered_report(session: AsyncSession, *, candidate_id: UUID, takeaway: str, object_key: str) -> bool:
    """Publish a successful render only while this worker still owns ``pending``."""
    result = await session.execute(text("""
        update candidate_report set takeaway=:takeaway,generated_at=now(),pdf_object_key=:object_key,
               pdf_status='available',pdf_rendered_at=now()
         where candidate_id=:candidate and pdf_status='pending'
    """), {"candidate": candidate_id, "takeaway": takeaway, "object_key": object_key})
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def mark_render_failed(session: AsyncSession, *, candidate_id: UUID) -> None:
    """Expose retryable render failure while preserving the dynamic report data."""
    await session.execute(text("""
        update candidate_report set pdf_status='failed'
         where candidate_id=:candidate and pdf_status='pending'
    """), {"candidate": candidate_id})


async def queue_eligible_candidate_report(session: AsyncSession, *, candidate_id: UUID) -> bool:
    """Create E-3 only after its policy condition and the PDF readiness gate hold."""
    row = (await session.execute(text("""
        select c.org_id,c.team_id
          from candidate c join position p on p.id=c.position_id
          join candidate_report cr on cr.candidate_id=c.id
         where c.id=:candidate and cr.pdf_status='available'
           and ((p.report_policy='after_finish' and c.completed_at is not null)
             or (p.report_policy='rejected_only' and c.decision='rejected'))
    """), {"candidate": candidate_id})).mappings().one_or_none()
    if row is None:
        return False
    email_send_id = (await session.execute(text("""
        insert into email_send (id,org_id,kind,dedupe_key,candidate_id)
        values (:id,:org,'E3_candidate_report',cast(:candidate as text),:candidate)
        on conflict (kind,dedupe_key) do nothing returning id
    """), {"id": new_id(), "org": row["org_id"], "candidate": candidate_id})).scalar_one_or_none()
    if email_send_id is None:
        return False
    await enqueue(session, Lane.DISPATCH_EMAIL, {"email_send_id": str(email_send_id)}, org_id=row["org_id"], team_id=row["team_id"])
    return True
