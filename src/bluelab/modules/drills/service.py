"""Domain services — this module's behaviour, and its published interface to the
other modules. Cross-module callers enter here; they never touch `models.py`.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.adapters.generation_llm import (
    GeneratedRubric,
    GeneratedScenario,
    GenerationProvider,
)
from bluelab.modules.drills.schemas import (
    AuthoringOption,
    BriefView,
    ConcealedEntryInput,
    DrillFull,
    DrillInputs,
    ReferenceView,
    RubricView,
)
from bluelab.platform.errors import catalog
from bluelab.platform.errors.denial import ProblemError, not_found
from bluelab.platform.ids import new_id
from bluelab.platform.queue.catalog import Lane
from bluelab.platform.queue.enqueue import enqueue

Kind = Literal["scenario", "rubric"]


async def archive_drill(
    session: AsyncSession,
    *,
    drill_id: UUID,
    org_id: UUID,
    team_id: UUID,
    viewer_id: UUID,
) -> None:
    """Archive a manager-owned published drill and withdraw future work.

    Existing attempts remain untouched.  The drill row is locked before the
    assignment is removed so archive, assignment, and assessment composition
    races converge on the archived state.
    """
    row = (
        await session.execute(
            text(
                "select status from drill where id=:drill and org_id=:org and "
                "team_id=:team and author_account_id=:viewer and not self_authored "
                "for update"
            ),
            {"drill": drill_id, "org": org_id, "team": team_id, "viewer": viewer_id},
        )
    ).scalar_one_or_none()
    if row is None:
        raise not_found()
    if row == "draft":
        raise ProblemError(catalog.DRILL_NOT_ARCHIVABLE)
    if row == "archived":
        return
    # A frozen position retains its historical assessment stages.  Admission
    # rejects archived drills; unfrozen future composition withdraws the stage.
    await session.execute(
        text(
            "delete from assessment_stage s using position p "
            "where s.position_id=p.id and s.drill_id=:drill "
            "and p.assessment_frozen_at is null"
        ),
        {"drill": drill_id},
    )
    await session.execute(text("delete from assignment where drill_id=:drill"), {"drill": drill_id})
    await session.execute(
        text(
            "update drill set status='archived', archived_at=now(), updated_at=now() "
            "where id=:drill and status='published'"
        ),
        {"drill": drill_id},
    )


async def list_options(
    session: AsyncSession, *, kind: str | None
) -> list[AuthoringOption]:
    rows = (
        (
            await session.execute(
                text(
                    "select id,kind,label from authoring_option where active and (:kind is null or kind=:kind) order by kind,label,id"
                ),
                {"kind": kind},
            )
        )
        .mappings()
        .all()
    )
    return [AuthoringOption(**row) for row in rows]


async def _resolve_entries(
    session: AsyncSession,
    entries: Sequence[ConcealedEntryInput],
    *,
    expected_kind: str,
    provider: GenerationProvider,
) -> list[dict[str, str]]:
    resolved: list[dict[str, str]] = []
    for index, entry in enumerate(entries):
        if entry.source == "library":
            row = (
                await session.execute(
                    text(
                        "select label from authoring_option where id=:id and kind=:kind and active"
                    ),
                    {"id": entry.option_id, "kind": expected_kind},
                )
            ).scalar_one_or_none()
            if row is None:
                raise not_found()
            resolved.append({"source": "library", "label": str(row)})
        else:
            label = (entry.label or "").strip()
            result = await provider.check_relevance(label)
            if not result.relevant:
                raise ProblemError(
                    catalog.NOT_JOB_RELEVANT,
                    detail=result.reason,
                    meta={
                        "errors": [
                            {
                                "field": f"{expected_kind}[{index}]",
                                "message": result.reason,
                            }
                        ]
                    },
                )
            resolved.append({"source": "custom", "label": label})
    return resolved


async def create_drill(
    session: AsyncSession,
    *,
    org_id: UUID,
    team_id: UUID,
    author_id: UUID,
    role: str,
    payload: DrillInputs,
    provider: GenerationProvider,
) -> UUID:
    challenges = await _resolve_entries(
        session, payload.challenges, expected_kind="challenge", provider=provider
    )
    motives = await _resolve_entries(
        session,
        payload.hidden_motives,
        expected_kind="hidden_motive",
        provider=provider,
    )
    drill_id = new_id()
    await session.execute(
        text(
            "insert into drill(id,org_id,team_id,author_account_id,self_authored,call_type,lead_type,language) values(:id,:org,:team,:author,:self_authored,:call_type,:lead_type,'ar-EG')"
        ),
        {
            "id": drill_id,
            "org": org_id,
            "team": team_id,
            "author": author_id,
            "self_authored": role != "manager",
            "call_type": payload.call_type,
            "lead_type": payload.lead_type,
        },
    )
    await session.execute(
        text(
            "insert into drill_concealed(drill_id,org_id,team_id,challenges,hidden_motives) values(:drill,:org,:team,cast(:challenges as jsonb),cast(:motives as jsonb))"
        ),
        {
            "drill": drill_id,
            "org": org_id,
            "team": team_id,
            "challenges": json.dumps(challenges),
            "motives": json.dumps(motives),
        },
    )
    return drill_id


async def replace_inputs(
    session: AsyncSession,
    *,
    drill_id: UUID,
    org_id: UUID,
    team_id: UUID,
    viewer_id: UUID,
    is_manager: bool,
    payload: DrillInputs,
    provider: GenerationProvider,
) -> None:
    challenges = await _resolve_entries(
        session, payload.challenges, expected_kind="challenge", provider=provider
    )
    motives = await _resolve_entries(
        session,
        payload.hidden_motives,
        expected_kind="hidden_motive",
        provider=provider,
    )
    visible = await _lock_draft(
        session,
        drill_id=drill_id,
        org_id=org_id,
        team_id=team_id,
        viewer_id=viewer_id,
        is_manager=is_manager,
    )
    if visible["status"] != "draft":
        raise ProblemError(catalog.DRILL_NOT_DRAFT)
    await session.execute(
        text(
            "delete from rubric_dimension where drill_id=:drill and org_id=:org and team_id=:team"
        ),
        {"drill": drill_id, "org": org_id, "team": team_id},
    )
    await session.execute(
        text(
            "update drill set call_type=:call_type,lead_type=:lead_type,label=null,scenario=null,draft_grounding=null,scenario_generation_status='none',scenario_generation_request_id=null,rubric_generation_status='none',rubric_generation_request_id=null,generation_error=null,updated_at=now() where id=:drill and org_id=:org and team_id=:team"
        ),
        {
            "drill": drill_id,
            "org": org_id,
            "team": team_id,
            "call_type": payload.call_type,
            "lead_type": payload.lead_type,
        },
    )
    await session.execute(
        text(
            "update drill_concealed set challenges=cast(:challenges as jsonb),hidden_motives=cast(:motives as jsonb) where drill_id=:drill and org_id=:org and team_id=:team"
        ),
        {
            "drill": drill_id,
            "org": org_id,
            "team": team_id,
            "challenges": json.dumps(challenges),
            "motives": json.dumps(motives),
        },
    )


async def _lock_draft(
    session: AsyncSession,
    *,
    drill_id: UUID,
    org_id: UUID,
    team_id: UUID,
    viewer_id: UUID,
    is_manager: bool,
) -> Any:
    row = (
        (
            await session.execute(
                text(
                    "select * from drill where id=:drill and org_id=:org and team_id=:team and (author_account_id=:viewer or (:manager and not self_authored)) for update"
                ),
                {
                    "drill": drill_id,
                    "org": org_id,
                    "team": team_id,
                    "viewer": viewer_id,
                    "manager": is_manager,
                },
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise not_found()
    return row


async def get_full(
    session: AsyncSession,
    *,
    drill_id: UUID,
    org_id: UUID,
    team_id: UUID,
    viewer_id: UUID,
    is_manager: bool,
) -> DrillFull:
    row = (
        (
            await session.execute(
                text(
                    "select d.*,c.challenges,c.hidden_motives from drill d join drill_concealed c on c.drill_id=d.id where d.id=:drill and d.org_id=:org and d.team_id=:team and (d.author_account_id=:viewer or (:manager and not d.self_authored))"
                ),
                {
                    "drill": drill_id,
                    "org": org_id,
                    "team": team_id,
                    "viewer": viewer_id,
                    "manager": is_manager,
                },
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise not_found()
    dimensions = (
        (
            await session.execute(
                text(
                    "select id,ord,name,weight,rationale from rubric_dimension where drill_id=:drill and org_id=:org and team_id=:team order by ord,id"
                ),
                {"drill": drill_id, "org": org_id, "team": team_id},
            )
        )
        .mappings()
        .all()
    )
    rubric = (
        RubricView.model_validate(
            {
                "dimensions": list(dimensions),
                "total": sum(int(item["weight"]) for item in dimensions),
            }
        )
        if dimensions
        else None
    )
    scenario = dict(row["scenario"]) if row["scenario"] else None
    if scenario:
        scenario.pop("v", None)
    return DrillFull.model_validate(
        {
            "id": row["id"],
            "status": row["status"],
            "self_authored": row["self_authored"],
            "author_account_id": row["author_account_id"],
            "call_type": row["call_type"],
            "lead_type": row["lead_type"],
            "language": row["language"],
            "label": row["label"],
            "inputs": {
                "challenges": row["challenges"],
                "hidden_motives": row["hidden_motives"],
            },
            "scenario": scenario,
            "rubric": rubric,
            "generation": {
                "scenario_status": row["scenario_generation_status"],
                "rubric_status": row["rubric_generation_status"],
                "error": row["generation_error"],
            },
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "published_at": row["published_at"],
            "archived_at": row["archived_at"],
        }
    )


async def _capture_grounding(
    session: AsyncSession, *, org_id: UUID, team_id: UUID
) -> dict[str, Any]:
    rows = (
        (
            await session.execute(
                text(
                    "select d.id document_id,d.live_version,d.title,"
                    "coalesce(s.published_by_account_id,s.created_by) published_by_account_id,"
                    "f.id fact_id,f.ord,f.label,f.value,f.note,clock.snapshot_at "
                    "from (select transaction_timestamp() snapshot_at) clock "
                    "left join (product_document d join fact_set s "
                    "on s.document_id=d.id and s.kind='live') "
                    "on d.org_id=:org and d.team_id=:team "
                    "left join product_fact f on f.fact_set_id=s.id "
                    "order by d.id,f.ord,f.id"
                ),
                {"org": org_id, "team": team_id},
            )
        )
        .mappings()
        .all()
    )
    documents: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    snapshot_at = None
    for row in rows:
        snapshot_at = row["snapshot_at"]
        if row["document_id"] is None:
            continue
        if current is None or current["document_id"] != str(row["document_id"]):
            current = {
                "document_id": str(row["document_id"]),
                "live_version": row["live_version"],
                "title": row["title"],
                "published_by_account_id": str(row["published_by_account_id"]),
                "facts": [],
            }
            documents.append(current)
        if row["fact_id"] is not None:
            current["facts"].append(
                {
                    "fact_id": str(row["fact_id"]),
                    "ord": row["ord"],
                    "label": row["label"],
                    "value": row["value"],
                    "note": row["note"],
                }
            )
    if snapshot_at is None:
        raise RuntimeError("grounding snapshot query returned no timestamp")
    return {
        "v": 1,
        "snapshot_at": snapshot_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "documents": documents,
    }


async def request_scenario(
    session: AsyncSession,
    *,
    drill_id: UUID,
    org_id: UUID,
    team_id: UUID,
    viewer_id: UUID,
    is_manager: bool,
) -> UUID:
    await session.execute(
        text("select pg_advisory_xact_lock(hashtextextended(cast(:team as text),0))"),
        {"team": str(team_id)},
    )
    row = await _lock_draft(
        session,
        drill_id=drill_id,
        org_id=org_id,
        team_id=team_id,
        viewer_id=viewer_id,
        is_manager=is_manager,
    )
    if row["status"] != "draft":
        raise ProblemError(catalog.DRILL_NOT_DRAFT)
    if (
        row["scenario_generation_status"] == "running"
        or row["rubric_generation_status"] == "running"
    ):
        raise ProblemError(catalog.GENERATION_IN_PROGRESS)
    concealed = (
        (
            await session.execute(
                text(
                    "select challenges,hidden_motives from drill_concealed where drill_id=:drill"
                ),
                {"drill": drill_id},
            )
        )
        .mappings()
        .one()
    )
    if not concealed["challenges"] or not concealed["hidden_motives"]:
        raise ProblemError(
            catalog.VALIDATION_ERROR,
            detail="At least one challenge and one hidden motive are required.",
        )
    grounding = await _capture_grounding(session, org_id=org_id, team_id=team_id)
    request_id = new_id()
    await session.execute(
        text("delete from rubric_dimension where drill_id=:drill"), {"drill": drill_id}
    )
    await session.execute(
        text(
            "update drill set draft_grounding=cast(:grounding as jsonb),scenario=null,label=null,scenario_generation_status='running',scenario_generation_request_id=:request,rubric_generation_status='none',rubric_generation_request_id=null,generation_error=null,updated_at=now() where id=:drill"
        ),
        {"drill": drill_id, "grounding": json.dumps(grounding), "request": request_id},
    )
    await enqueue(
        session,
        Lane.GENERATE_SCENARIO,
        {"drill_id": str(drill_id), "request_id": str(request_id)},
        org_id=org_id,
        team_id=team_id,
    )
    return request_id


async def request_rubric(
    session: AsyncSession,
    *,
    drill_id: UUID,
    org_id: UUID,
    team_id: UUID,
    viewer_id: UUID,
    is_manager: bool,
    discard_confirmed: bool | None,
) -> UUID:
    row = await _lock_draft(
        session,
        drill_id=drill_id,
        org_id=org_id,
        team_id=team_id,
        viewer_id=viewer_id,
        is_manager=is_manager,
    )
    if row["status"] != "draft":
        raise ProblemError(catalog.DRILL_NOT_DRAFT)
    if (
        row["scenario_generation_status"] == "running"
        or row["rubric_generation_status"] == "running"
    ):
        raise ProblemError(catalog.GENERATION_IN_PROGRESS)
    if row["scenario"] is None or row["draft_grounding"] is None:
        raise ProblemError(catalog.GENERATION_INCOMPLETE)
    count = (
        await session.execute(
            text("select count(*) from rubric_dimension where drill_id=:drill"),
            {"drill": drill_id},
        )
    ).scalar_one()
    if count and discard_confirmed is not True:
        raise ProblemError(
            catalog.VALIDATION_ERROR, detail="Confirm discarding the current rubric."
        )
    await session.execute(
        text("delete from rubric_dimension where drill_id=:drill"), {"drill": drill_id}
    )
    request_id = new_id()
    await session.execute(
        text(
            "update drill set rubric_generation_status='running',rubric_generation_request_id=:request,generation_error=null,updated_at=now() where id=:drill"
        ),
        {"drill": drill_id, "request": request_id},
    )
    await enqueue(
        session,
        Lane.GENERATE_RUBRIC,
        {"drill_id": str(drill_id), "request_id": str(request_id)},
        org_id=org_id,
        team_id=team_id,
    )
    return request_id


async def generation_basis(
    session: AsyncSession, *, drill_id: UUID, request_id: UUID, kind: Kind
) -> dict[str, Any] | None:
    request_column = f"{kind}_generation_request_id"
    status_column = f"{kind}_generation_status"
    row = (
        (
            await session.execute(
                text(
                    f"select d.call_type,d.lead_type,d.language,d.scenario,d.draft_grounding,c.challenges,c.hidden_motives from drill d join drill_concealed c on c.drill_id=d.id where d.id=:drill and d.{request_column}=:request and d.{status_column}='running'"
                ),
                {"drill": drill_id, "request": request_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    return {
        "call_type": row["call_type"],
        "lead_type": row["lead_type"],
        "language": row["language"],
        "custom_entries": {
            "challenges": row["challenges"],
            "hidden_motives": row["hidden_motives"],
        },
        "scenario": row["scenario"],
        "grounding": row["draft_grounding"],
    }


async def apply_scenario(
    session: AsyncSession,
    *,
    drill_id: UUID,
    request_id: UUID,
    result: GeneratedScenario,
) -> bool:
    payload = {"v": 1, **result.model_dump(exclude={"label"})}
    updated = await session.execute(
        text(
            "update drill set scenario=cast(:scenario as jsonb),label=:label,scenario_generation_status='succeeded',scenario_generation_request_id=null,generation_error=null,updated_at=now() where id=:drill and status='draft' and scenario_generation_status='running' and scenario_generation_request_id=:request"
        ),
        {
            "drill": drill_id,
            "request": request_id,
            "scenario": json.dumps(payload),
            "label": result.label,
        },
    )
    return bool(getattr(updated, "rowcount", 0) == 1)


async def apply_rubric(
    session: AsyncSession, *, drill_id: UUID, request_id: UUID, result: GeneratedRubric
) -> bool:
    row = (
        (
            await session.execute(
                text(
                    "select org_id,team_id from drill where id=:drill and status='draft' and rubric_generation_status='running' and rubric_generation_request_id=:request for update"
                ),
                {"drill": drill_id, "request": request_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return False
    await session.execute(
        text("delete from rubric_dimension where drill_id=:drill"), {"drill": drill_id}
    )
    for ordinal, dimension in enumerate(result.dimensions, 1):
        await session.execute(
            text(
                "insert into rubric_dimension(id,org_id,team_id,drill_id,ord,name,weight,rationale) values(:id,:org,:team,:drill,:ord,:name,:weight,:rationale)"
            ),
            {
                "id": new_id(),
                "org": row["org_id"],
                "team": row["team_id"],
                "drill": drill_id,
                "ord": ordinal,
                "name": dimension.name,
                "weight": dimension.weight,
                "rationale": dimension.rationale,
            },
        )
    await session.execute(
        text(
            "update drill set rubric_generation_status='succeeded',rubric_generation_request_id=null,generation_error=null,updated_at=now() where id=:drill and rubric_generation_request_id=:request"
        ),
        {"drill": drill_id, "request": request_id},
    )
    return True


async def mark_generation_failed(
    session: AsyncSession, *, drill_id: UUID, request_id: UUID, kind: Kind
) -> None:
    await session.execute(
        text(
            f"update drill set {kind}_generation_status='failed',{kind}_generation_request_id=null,generation_error='Generation failed. Try again.',updated_at=now() where id=:drill and {kind}_generation_status='running' and {kind}_generation_request_id=:request"
        ),
        {"drill": drill_id, "request": request_id},
    )


async def tune_weights(
    session: AsyncSession,
    *,
    drill_id: UUID,
    org_id: UUID,
    team_id: UUID,
    viewer_id: UUID,
    is_manager: bool,
    weights: Sequence[tuple[UUID, int]],
) -> RubricView:
    row = await _lock_draft(
        session,
        drill_id=drill_id,
        org_id=org_id,
        team_id=team_id,
        viewer_id=viewer_id,
        is_manager=is_manager,
    )
    if row["status"] != "draft":
        raise ProblemError(catalog.DRILL_NOT_DRAFT)
    for dimension_id, weight in weights:
        updated = await session.execute(
            text(
                "update rubric_dimension set weight=:weight where id=:dimension and drill_id=:drill and org_id=:org and team_id=:team"
            ),
            {
                "weight": weight,
                "dimension": dimension_id,
                "drill": drill_id,
                "org": org_id,
                "team": team_id,
            },
        )
        if updated.rowcount != 1:  # type: ignore[attr-defined]
            raise not_found()
    return await get_rubric(session, drill_id=drill_id, org_id=org_id, team_id=team_id)


async def delete_dimension(
    session: AsyncSession,
    *,
    drill_id: UUID,
    dimension_id: UUID,
    org_id: UUID,
    team_id: UUID,
    viewer_id: UUID,
    is_manager: bool,
) -> RubricView:
    row = await _lock_draft(
        session,
        drill_id=drill_id,
        org_id=org_id,
        team_id=team_id,
        viewer_id=viewer_id,
        is_manager=is_manager,
    )
    if row["status"] != "draft":
        raise ProblemError(catalog.DRILL_NOT_DRAFT)
    deleted = await session.execute(
        text(
            "delete from rubric_dimension where id=:dimension and drill_id=:drill and org_id=:org and team_id=:team"
        ),
        {"dimension": dimension_id, "drill": drill_id, "org": org_id, "team": team_id},
    )
    if deleted.rowcount != 1:  # type: ignore[attr-defined]
        raise not_found()
    return await get_rubric(session, drill_id=drill_id, org_id=org_id, team_id=team_id)


async def get_rubric(
    session: AsyncSession, *, drill_id: UUID, org_id: UUID, team_id: UUID
) -> RubricView:
    rows = (
        (
            await session.execute(
                text(
                    "select id,ord,name,weight,rationale from rubric_dimension where drill_id=:drill and org_id=:org and team_id=:team order by ord,id"
                ),
                {"drill": drill_id, "org": org_id, "team": team_id},
            )
        )
        .mappings()
        .all()
    )
    return RubricView.model_validate(
        {"dimensions": list(rows), "total": sum(int(row["weight"]) for row in rows)}
    )


def _reference_from_snapshot(
    drill_id: UUID,
    snapshot: dict[str, Any],
    *,
    frozen: bool,
    publishers: dict[str, str],
) -> ReferenceView:
    documents = snapshot.get("documents", [])
    groups = [
        {
            "document_title": document["title"],
            "facts": [
                {
                    "label": fact["label"],
                    "value": fact["value"],
                    "note": fact.get("note"),
                }
                for fact in document.get("facts", [])
            ],
        }
        for document in documents
    ]
    publisher_ids = sorted(
        {
            str(document["published_by_account_id"])
            for document in documents
            if document.get("published_by_account_id")
        }
    )
    publisher_names = [publishers.get(account_id, "—") for account_id in publisher_ids]
    return ReferenceView.model_validate(
        {
            "drill_id": drill_id,
            "frozen": frozen,
            "groups": groups,
            "provenance": {
                "source_documents": [document["title"] for document in documents],
                "published_by": " · ".join(publisher_names) if publisher_names else "—",
                "fact_count": sum(
                    len(document.get("facts", [])) for document in documents
                ),
                "snapshot_at": snapshot.get("snapshot_at"),
            },
        }
    )


async def reference(
    session: AsyncSession,
    *,
    drill_id: UUID,
    org_id: UUID,
    team_id: UUID,
    viewer_id: UUID,
) -> ReferenceView:
    row = (
        (
            await session.execute(
                text(
                    "select id,status,self_authored,author_account_id,scenario,draft_grounding,answer_key from drill where id=:drill and org_id=:org and team_id=:team and ((not self_authored) or author_account_id=:viewer)"
                ),
                {
                    "drill": drill_id,
                    "org": org_id,
                    "team": team_id,
                    "viewer": viewer_id,
                },
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise not_found()
    if row["status"] == "draft" and row["author_account_id"] != viewer_id:
        raise not_found()
    snapshot = row["answer_key"] if row["status"] != "draft" else row["draft_grounding"]
    if row["scenario"] is None or snapshot is None:
        raise ProblemError(catalog.GENERATION_INCOMPLETE)
    ids = [
        UUID(document["published_by_account_id"])
        for document in snapshot.get("documents", [])
        if document.get("published_by_account_id")
    ]
    names: dict[str, str] = {}
    if ids:
        for account in (
            await session.execute(
                text(
                    "select id,display_name from account where org_id=:org and id=any(:ids)"
                ),
                {"org": org_id, "ids": ids},
            )
        ).mappings():
            names[str(account["id"])] = account["display_name"]
    return _reference_from_snapshot(
        drill_id, snapshot, frozen=row["status"] != "draft", publishers=names
    )


async def brief(
    session: AsyncSession,
    *,
    drill_id: UUID,
    org_id: UUID,
    team_id: UUID,
    viewer_id: UUID,
) -> BriefView:
    row = (
        (
            await session.execute(
                text(
                    "select d.*,c.challenges,c.hidden_motives from drill d join drill_concealed c on c.drill_id=d.id where d.id=:drill and d.org_id=:org and d.team_id=:team and d.status in ('published','archived') and ((not d.self_authored) or d.author_account_id=:viewer)"
                ),
                {
                    "drill": drill_id,
                    "org": org_id,
                    "team": team_id,
                    "viewer": viewer_id,
                },
            )
        )
        .mappings()
        .first()
    )
    if row is None or row["scenario"] is None or row["answer_key"] is None:
        raise not_found()
    scenario = row["scenario"]
    facts = [
        fact["value"]
        for document in row["answer_key"].get("documents", [])
        for fact in document.get("facts", [])
    ]
    recap = None
    if row["author_account_id"] == viewer_id:
        recap = {
            "challenges": [item["label"] for item in row["challenges"]],
            "hidden_motives": [item["label"] for item in row["hidden_motives"]],
        }
    return BriefView(
        drill_id=drill_id,
        label=row["label"],
        call_type=row["call_type"],
        lead_type=row["lead_type"],
        language=row["language"],
        buyer=scenario["persona"],
        context=scenario["context"],
        product_summary=" ".join(facts[:3]),
        author_recap=recap,
    )
