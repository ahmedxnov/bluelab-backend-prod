"""Position, assessment composition, candidate entry and pipeline readers."""

from __future__ import annotations

import csv
import io
from collections.abc import Sequence
from typing import Any, cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.modules.hiring.schemas import (
    CandidateCreate,
    ParsedCandidateRow,
    PositionCreate,
    PositionPatch,
)
from bluelab.platform.errors import catalog
from bluelab.platform.errors.denial import ProblemError, ValidationProblem, not_found
from bluelab.platform.ids import new_id


async def create_position(session: AsyncSession, *, org_id: UUID, team_id: UUID, payload: PositionCreate) -> UUID:
    position_id = new_id()
    await session.execute(text("""insert into position
      (id,org_id,team_id,title,openings,notify_on_completion,report_policy)
      values (:id,:org,:team,:title,:openings,:notify,:policy)"""),
      {"id": position_id, "org": org_id, "team": team_id, "title": payload.title.strip(),
       "openings": payload.openings, "notify": payload.notify_on_completion, "policy": payload.report_policy})
    return position_id


async def _position_row(
    session: AsyncSession, position_id: UUID, *, for_update: bool = False
) -> dict[str, Any]:
    lock = " for update" if for_update else ""
    row = (await session.execute(
        text("select p.* from position p where p.id=:id" + lock), {"id": position_id}
    )).mappings().one_or_none()
    if row is None:
        raise not_found()
    return dict(row)


async def position_detail(session: AsyncSession, position_id: UUID) -> dict[str, Any]:
    pos = await _position_row(session, position_id)
    counts = (await session.execute(text("""
      select invited::int,pending_review::int,approved_unsent::int
        from v_position_counts where position_id=:id
    """), {"id": position_id})).mappings().one()
    stages = (await session.execute(text("""select s.id, s.ord, d.id as drill_id,d.label,d.call_type
      from assessment_stage s join drill d on d.id=s.drill_id
      where s.position_id=:id order by s.ord"""), {"id": position_id})).mappings().all()
    return {"id": pos["id"], "title": pos["title"], "openings": pos["openings"], "status": pos["status"],
       "counts": dict(counts), "created_at": pos["created_at"],
       "notify_on_completion": pos["notify_on_completion"], "report_policy": pos["report_policy"],
       "invite_expiry_days": pos["invite_expiry_days"], "invite_template": pos["invite_template"],
       "language": "ar-EG", "assessment_frozen": pos["assessment_frozen_at"] is not None,
       "stages": [{"stage_id": r["id"], "ord": r["ord"], "drill": {"id": r["drill_id"], "label": r["label"], "call_type": r["call_type"]}} for r in stages],
       "closed_at": pos["closed_at"]}


async def patch_position(session: AsyncSession, *, position_id: UUID, payload: PositionPatch) -> None:
    pos = await _position_row(session, position_id)
    if pos["status"] == "closed":
        raise ProblemError(catalog.POSITION_CLOSED)
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        return
    assignments = ", ".join(f"{name}=:{name}" for name in fields)
    fields["id"] = position_id
    await session.execute(text(f"update position set {assignments}, updated_at=now() where id=:id"), fields)


async def replace_assessment(session: AsyncSession, *, position_id: UUID, drill_ids: Sequence[UUID]) -> None:
    # Lock the referenced drills before the position. Archive takes the drill
    # lock before withdrawing stages, so neither path can persist a new stage
    # against an archived drill after checking its prior published state.
    if drill_ids:
        await session.execute(
            text(
                "select id from drill where id=any(cast(:ids as uuid[])) "
                "order by id for update"
            ),
            {"ids": list(drill_ids)},
        )
    # The position row is the serialization point shared with T-7.  Without a
    # lock, an editor could delete a stage after an invite checked for one and
    # before that invite marked the assessment frozen.
    pos = await _position_row(session, position_id, for_update=True)
    if pos["status"] == "closed":
        raise ProblemError(catalog.POSITION_CLOSED)
    if pos["assessment_frozen_at"] is not None:
        raise ProblemError(catalog.ASSESSMENT_FROZEN)
    if len(set(drill_ids)) != len(drill_ids):
        raise ValidationProblem([{"field": "drill_ids", "message": "each drill may appear once"}])
    published = (await session.execute(text("""select id from drill where team_id=:team and status='published'
      and self_authored=false and id = any(:ids)"""), {"team": pos["team_id"], "ids": list(drill_ids)})).scalars().all()
    if len(published) != len(drill_ids):
        raise ProblemError(catalog.DRILL_NOT_STARTABLE)
    await session.execute(text("delete from assessment_stage where position_id=:id"), {"id": position_id})
    for ordinal, drill_id in enumerate(drill_ids, 1):
        await session.execute(text("""insert into assessment_stage (id,org_id,team_id,position_id,ord,drill_id)
          values (:id,:org,:team,:position,:ord,:drill)"""),
          {"id": new_id(), "org": pos["org_id"], "team": pos["team_id"], "position": position_id, "ord": ordinal, "drill": drill_id})
    await session.execute(text("update position set status='active',updated_at=now() where id=:id"), {"id": position_id})


async def add_candidates(session: AsyncSession, *, position_id: UUID, candidates: Sequence[CandidateCreate]) -> list[dict[str, Any]]:
    pos = await _position_row(session, position_id)
    if pos["status"] == "closed":
        raise ProblemError(catalog.POSITION_CLOSED)
    normalized = [str(candidate.email).lower() for candidate in candidates]
    if len(normalized) != len(set(normalized)):
        raise ValidationProblem([{"field": "candidates", "message": "email appears more than once"}])
    existing = (await session.execute(text("select lower(email) from candidate where position_id=:position and lower(email)=any(:emails)"), {"position": position_id, "emails": normalized})).scalars().all()
    if existing:
        raise ProblemError(catalog.DUPLICATE_EMAIL)
    created: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate_id = new_id()
        email = str(candidate.email).lower()
        await session.execute(text("""insert into candidate
          (id,org_id,team_id,position_id,name,email,phone,linkedin,source,internal_note)
          values (:id,:org,:team,:position,:name,:email,:phone,:linkedin,:source,:note)"""),
          {"id": candidate_id,"org":pos["org_id"],"team":pos["team_id"],"position":position_id,"name":candidate.name.strip(),"email":email,
           "phone":candidate.phone,"linkedin":candidate.linkedin,"source":candidate.source,"note":candidate.internal_note})
        created.append({"candidate_id": candidate_id, "name": candidate.name.strip(), "email": email})
    return created


_CSV_FIELDS = ("name", "email", "phone", "linkedin", "source")


def parse_candidate_csv(raw: bytes, *, content_type: str | None) -> list[ParsedCandidateRow]:
    if content_type and content_type not in {"text/csv", "application/vnd.ms-excel", "application/octet-stream"}:
        raise ProblemError(catalog.UNSUPPORTED_FILE_TYPE)
    if len(raw) > 2_000_000:
        raise ValidationProblem([{"field": "file", "message": "file is too large"}])
    try:
        decoded = raw.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(decoded))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise ValidationProblem([{"field": "file", "message": "invalid CSV template"}]) from exc
    if reader.fieldnames != list(_CSV_FIELDS):
        raise ProblemError(catalog.UNSUPPORTED_FILE_TYPE)
    rows: list[ParsedCandidateRow] = []
    for index, source in enumerate(reader, 2):
        if index > 102: raise ValidationProblem([{"field": "file", "message": "maximum 100 rows"}])
        values = {field: (source.get(field) or "").strip() or None for field in _CSV_FIELDS}
        problems: list[str] = []
        if not values["name"]: problems.append("name is required")
        if not values["email"] or "@" not in values["email"]: problems.append("valid email is required")
        # Values are returned as text for review only. They are never evaluated,
        # rendered as HTML, or inserted until the reviewed JSON batch is posted.
        rows.append(ParsedCandidateRow(**values, problems=problems))
    return rows


async def list_positions(session: AsyncSession) -> dict[str, Any]:
    rows = (await session.execute(text("""
      select p.id,p.title,p.openings,p.status,p.created_at,pc.invited::int,pc.pending_review::int,
             pc.approved_unsent::int
        from position p join v_position_counts pc on pc.position_id=p.id
       order by p.created_at desc,p.id desc
    """))).mappings().all()
    totals = (await session.execute(text("""
      select count(*) filter(where p.status <> 'closed')::int as open_positions,
             coalesce(sum(pc.pending_review) filter(where p.status <> 'closed'),0)::int as pending_review,
             coalesce(sum(pc.approved_unsent) filter(where p.status <> 'closed'),0)::int as approved_unsent
        from position p join v_position_counts pc on pc.position_id=p.id
    """))).mappings().one()
    data: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["counts"] = {
            "invited": item.pop("invited"),
            "pending_review": item.pop("pending_review"),
            "approved_unsent": item.pop("approved_unsent"),
        }
        data.append(item)
    return {"data": data, "totals": dict(totals), "pagination": {"next_cursor": None}}


async def pipeline(session: AsyncSession, *, position_id: UUID, view: str) -> dict[str, Any]:
    await _position_row(session, position_id)
    counts = (await session.execute(text("""select count(*)::int pipeline,
      count(*) filter(where completed_at is not null)::int review_queue,
      count(*) filter(where decision='approved')::int approved
      from candidate where position_id=:position"""), {"position": position_id})).mappings().one()
    clause = "" if view == "pipeline" else ("and c.completed_at is not null" if view == "review_queue" else "and c.decision='approved'")
    rows = (await session.execute(text(f"""select c.id candidate_id,c.name,c.email,c.phone,c.linkedin,c.source,c.internal_note,
      v.journey_state,v.expired_incomplete,c.decision,exists(select 1 from shortlist_candidate sc where sc.candidate_id=c.id) decision_frozen,
      o.overall_score,case when o.overall_score is null then null else fn_score_band(o.overall_score) end overall_band,coalesce(o.any_restart,false) any_restart,
      e.created_at last_sent_at,e.status delivery_status,t.expires_at
      from candidate c join v_candidate_state v on v.id=c.id left join v_candidate_overall o on o.candidate_id=c.id
      left join lateral (select es.created_at,es.status,ct.expires_at from email_send es join candidate_token ct on ct.id=es.token_id where es.candidate_id=c.id and es.kind='E2_invite' order by es.created_at desc limit 1) e on true
      left join lateral (select ct.expires_at from candidate_token ct join email_send es on es.token_id=ct.id where es.candidate_id=c.id and es.kind='E2_invite' order by es.created_at desc limit 1) t on true
      where c.position_id=:position {clause} order by o.overall_score desc nulls last,c.created_at desc,c.id desc"""), {"position": position_id})).mappings().all()
    data=[]
    for r in rows:
        item = dict(r)
        overall_score = item.pop("overall_score")
        overall_band = item.pop("overall_band")
        item["overall"] = None if overall_score is None else {"score": float(overall_score), "band": overall_band}
        last_sent_at = item.pop("last_sent_at")
        delivery_status = item.pop("delivery_status")
        expires_at = item.pop("expires_at")
        item["invite"] = None if last_sent_at is None else {
            "last_sent_at": last_sent_at,
            "delivery_status": delivery_status,
            "expires_at": expires_at,
        }
        data.append(item)
    if any(item["expired_incomplete"] for item in data):
        # V-10 derives expiry from valid tokens at read time.  No lifecycle row
        # is rewritten, which preserves completed evidence for reporting.
        from bluelab.platform.telemetry import metrics
        metrics.record_candidate_probe(kind="expiry_reconciled")
    return {"view": view,"counts":dict(counts),"data":data,"pagination":{"next_cursor":None}}


async def update_note(session: AsyncSession, *, candidate_id: UUID, internal_note: str | None) -> UUID:
    row=(await session.execute(text("""update candidate c set internal_note=:note,updated_at=now()
      from position p where c.id=:id and p.id=c.position_id and p.status <> 'closed' returning c.position_id"""), {"id":candidate_id,"note":internal_note})).scalar_one_or_none()
    if row is None:
        # Under RLS both absent and a foreign candidate reach this same path.
        raise not_found()
    return cast(UUID, row)
