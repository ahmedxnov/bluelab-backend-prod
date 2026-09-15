"""Manager hiring routes; every operation is scoped to the owning manager."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Header, Query, UploadFile, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy import text

from bluelab.api.deps import (
    DeliverySecretSealerDep,
    ManagerPrincipal,
    ObjectStoreDep,
    scope_of,
)
from bluelab.modules.hiring import invites, reports, service, shortlist
from bluelab.modules.hiring.schemas import (
    AssessmentPut,
    CandidateBatch,
    CandidateDecision,
    CandidatePatch,
    HrContactCreate,
    InviteBatch,
    ParseResult,
    PipelinePage,
    PositionCreate,
    PositionDetail,
    PositionList,
    PositionPatch,
    ShortlistSend,
)
from bluelab.platform.db.session import scoped_transaction
from bluelab.platform.errors import catalog
from bluelab.platform.errors.denial import ProblemError, ValidationProblem, not_found
from bluelab.platform.http.idempotency import (
    acquire_replay_lock,
    load_replay,
    require_key,
    store_response,
)
from bluelab.platform.telemetry import metrics

router = APIRouter(tags=["Hiring"])


@router.get("/positions", operation_id="listPositions", response_model=PositionList)
async def list_positions(record: ManagerPrincipal) -> dict[str, object]:
    async with scoped_transaction(scope_of(record)) as db:
        return await service.list_positions(db)


@router.post("/positions", operation_id="createPosition", response_model=PositionDetail, status_code=status.HTTP_201_CREATED)
async def create_position(payload: PositionCreate, record: ManagerPrincipal) -> dict[str, object]:
    scope = scope_of(record)
    async with scoped_transaction(scope) as db:
        position_id = await service.create_position(db, org_id=scope.org_id, team_id=scope.team_id, payload=payload)  # type: ignore[arg-type]
        return await service.position_detail(db, position_id)


@router.get("/positions/{position_id}", operation_id="getPosition", response_model=PositionDetail)
async def get_position(position_id: UUID, record: ManagerPrincipal) -> dict[str, object]:
    async with scoped_transaction(scope_of(record)) as db:
        return await service.position_detail(db, position_id)


@router.patch("/positions/{position_id}", operation_id="updatePosition", response_model=PositionDetail)
async def update_position(position_id: UUID, payload: PositionPatch, record: ManagerPrincipal) -> dict[str, object]:
    async with scoped_transaction(scope_of(record)) as db:
        await service.patch_position(db, position_id=position_id, payload=payload)
        return await service.position_detail(db, position_id)


@router.put("/positions/{position_id}/assessment", operation_id="putAssessment", response_model=PositionDetail)
async def put_assessment(position_id: UUID, payload: AssessmentPut, record: ManagerPrincipal) -> dict[str, object]:
    async with scoped_transaction(scope_of(record)) as db:
        await service.replace_assessment(db, position_id=position_id, drill_ids=payload.drill_ids)
        return await service.position_detail(db, position_id)


@router.get("/positions/{position_id}/candidates", operation_id="listPositionCandidates", response_model=PipelinePage)
async def list_candidates(position_id: UUID, record: ManagerPrincipal, view: Annotated[str, Query()] = "pipeline") -> dict[str, object]:
    async with scoped_transaction(scope_of(record)) as db:
        return await service.pipeline(db, position_id=position_id, view=view)


@router.post("/positions/{position_id}/candidates", operation_id="addCandidates", status_code=status.HTTP_201_CREATED)
async def add_candidates(position_id: UUID, payload: CandidateBatch, record: ManagerPrincipal,
                         idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> JSONResponse:
    scope = scope_of(record); body = payload.model_dump(mode="json")
    key = require_key(idempotency_key, org_id=scope.org_id, principal_id=scope.account_id, endpoint=f"positions/{position_id}/candidates")  # type: ignore[arg-type]
    async with scoped_transaction(scope) as db:
        await acquire_replay_lock(db, key=key)
        replay = await load_replay(db, key=key, body=body)
        if replay: return JSONResponse(replay.body, status_code=replay.status)
        response = jsonable_encoder({"created": await service.add_candidates(db, position_id=position_id, candidates=payload.candidates)})
        await store_response(db, key=key, body=body, status=201, response=response)
        return JSONResponse(response, status_code=201)


@router.post("/positions/{position_id}/candidates/parse", operation_id="parseCandidateFile", response_model=ParseResult)
async def parse_candidates(position_id: UUID, record: ManagerPrincipal, file: Annotated[UploadFile, File()]) -> dict[str, object]:
    async with scoped_transaction(scope_of(record)) as db:
        await service.position_detail(db, position_id)
        try:
            rows = service.parse_candidate_csv(await file.read(), content_type=file.content_type)
        except Exception:
            # A content-free outcome signal: no filename, candidate row, or
            # parser exception reaches telemetry.
            metrics.record_candidate_probe(kind="import_failed")
            raise
        return {"rows": rows}


@router.post("/positions/{position_id}/invites", operation_id="sendInvites", status_code=status.HTTP_202_ACCEPTED)
async def send_invites(position_id: UUID, payload: InviteBatch, record: ManagerPrincipal, sealer: DeliverySecretSealerDep,
                       idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> JSONResponse:
    scope = scope_of(record); body = payload.model_dump(mode="json")
    key = require_key(idempotency_key, org_id=scope.org_id, principal_id=scope.account_id, endpoint=f"positions/{position_id}/invites")  # type: ignore[arg-type]
    async with scoped_transaction(scope) as db:
        await acquire_replay_lock(db, key=key)
        replay = await load_replay(db, key=key, body=body)
        if replay: return JSONResponse(replay.body, status_code=replay.status)
        issued = await invites.send_invites(db, position_id=position_id, org_id=scope.org_id, team_id=scope.team_id, candidate_ids=payload.candidate_ids, sealer=sealer, expiry_days_override=payload.expiry_days, invite_template=payload.invite_template)  # type: ignore[arg-type]
        response = {"invites_queued": len(issued), "assessment_frozen": True}
        await store_response(db, key=key, body=body, status=202, response=response)
        return JSONResponse(response, status_code=202)


@router.patch("/candidates/{candidate_id}", operation_id="updateCandidate", response_model=dict)
async def update_candidate(candidate_id: UUID, payload: CandidatePatch, record: ManagerPrincipal) -> dict[str, object]:
    async with scoped_transaction(scope_of(record)) as db:
        position_id = await service.update_note(db, candidate_id=candidate_id, internal_note=payload.internal_note)
        page = await service.pipeline(db, position_id=position_id, view="pipeline")
        return next(row for row in page["data"] if row["candidate_id"] == candidate_id)


@router.post("/candidates/{candidate_id}/invite-resend", operation_id="resendInvite", status_code=status.HTTP_202_ACCEPTED)
async def resend_invite(candidate_id: UUID, record: ManagerPrincipal, sealer: DeliverySecretSealerDep,
                        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> JSONResponse:
    scope=scope_of(record)
    key = require_key(idempotency_key, org_id=scope.org_id, principal_id=scope.account_id, endpoint=f"candidates/{candidate_id}/invite-resend")  # type: ignore[arg-type]
    async with scoped_transaction(scope) as db:
        await acquire_replay_lock(db, key=key)
        replay = await load_replay(db, key=key, body={})
        if replay:
            return JSONResponse(replay.body, status_code=replay.status)
        row=(await db.execute(text("select position_id from candidate where id=:id and completed_at is null"), {"id":candidate_id})).scalar_one_or_none()
        if row is None: raise not_found()
        issued=await invites.send_invites(db, position_id=row, org_id=scope.org_id, team_id=scope.team_id, candidate_ids=[candidate_id], sealer=sealer)  # type: ignore[arg-type]
        expires_at=(await db.execute(text("select expires_at from candidate_token where id=:id"), {"id":issued[0].token_id})).scalar_one()
        response = jsonable_encoder({"expires_at": expires_at})
        await store_response(db, key=key, body={}, status=202, response=response)
        return JSONResponse(response, status_code=202)


@router.get("/candidates/{candidate_id}/report", operation_id="getCandidateReport")
async def get_candidate_report(candidate_id: UUID, record: ManagerPrincipal) -> dict[str, object]:
    async with scoped_transaction(scope_of(record)) as db:
        return await reports.manager_report(db, candidate_id=candidate_id)


@router.get("/candidates/{candidate_id}/report/pdf", operation_id="getReportPdf")
async def get_report_pdf(candidate_id: UUID, record: ManagerPrincipal, object_store: ObjectStoreDep) -> dict[str, object]:
    async with scoped_transaction(scope_of(record)) as db:
        url, expires_at = await reports.report_download(db, candidate_id=candidate_id, principal_id=UUID(record.account_id), object_store=object_store, authorization_seconds=300)
        return {"url": url, "expires_at": expires_at}


@router.post("/candidates/{candidate_id}/report/render", operation_id="renderReportPdf", status_code=status.HTTP_202_ACCEPTED)
async def render_report(candidate_id: UUID, record: ManagerPrincipal) -> dict[str, str]:
    scope = scope_of(record)
    async with scoped_transaction(scope) as db:
        pdf_status, claimed = await reports.request_render(db, candidate_id=candidate_id)
        if claimed:
            from bluelab.platform.queue.catalog import Lane
            from bluelab.platform.queue.enqueue import enqueue
            await enqueue(db, Lane.RENDER_REPORT, {"candidate_id": str(candidate_id)}, org_id=scope.org_id, team_id=scope.team_id)  # type: ignore[arg-type]
        return {"pdf_status": pdf_status}


@router.put("/candidates/{candidate_id}/decision", operation_id="putDecision")
async def put_decision(candidate_id: UUID, payload: CandidateDecision, record: ManagerPrincipal) -> dict[str, object]:
    async with scoped_transaction(scope_of(record)) as db:
        await shortlist.decide(db, candidate_id=candidate_id, decision=payload.decision)
        await reports.queue_eligible_candidate_report(db, candidate_id=candidate_id)
        row = (await db.execute(text("select position_id from candidate where id=:id"), {"id": candidate_id})).scalar_one_or_none()
        if row is None: raise not_found()
        page = await service.pipeline(db, position_id=row, view="pipeline")
        return next(item for item in page["data"] if item["candidate_id"] == candidate_id)


@router.get("/hr-contacts", operation_id="listHrContacts")
async def list_hr_contacts(record: ManagerPrincipal) -> dict[str, object]:
    scope = scope_of(record)
    async with scoped_transaction(scope) as db:
        contacts = (await db.execute(text("select id,email,label,created_at from hr_contact where owner_account_id=:owner order by created_at,id"), {"owner": scope.account_id})).mappings().all()
        domains = (await db.execute(text("select distinct lower(split_part(value,'@',2)) domain from shortlist s cross join lateral jsonb_array_elements_text(s.recipients) value where s.sent_by=:owner"), {"owner": scope.account_id})).scalars().all()
        return {"data": [dict(item) for item in contacts], "known_recipient_domains": list(domains)}


@router.post("/hr-contacts", operation_id="createHrContact", status_code=status.HTTP_201_CREATED)
async def create_hr_contact(payload: HrContactCreate, record: ManagerPrincipal) -> dict[str, object]:
    from bluelab.adapters.email import validate_address
    from bluelab.platform.ids import new_id
    scope = scope_of(record); email = validate_address(str(payload.email)).lower()
    async with scoped_transaction(scope) as db:
        row = (await db.execute(text("insert into hr_contact(id,org_id,owner_account_id,email,label) values(:id,:org,:owner,:email,:label) on conflict(owner_account_id,lower(email)) do nothing returning id,email,label,created_at"), {"id":new_id(),"org":scope.org_id,"owner":scope.account_id,"email":email,"label":payload.label})).mappings().one_or_none()
        if row is None: raise ProblemError(catalog.DUPLICATE_EMAIL)
        return dict(row)


@router.delete("/hr-contacts/{contact_id}", operation_id="deleteHrContact", status_code=status.HTTP_204_NO_CONTENT)
async def delete_hr_contact(contact_id: UUID, record: ManagerPrincipal) -> None:
    async with scoped_transaction(scope_of(record)) as db:
        deleted = await db.execute(text("delete from hr_contact where id=:id"), {"id": contact_id})
        if deleted.rowcount == 0: raise not_found()  # type: ignore[attr-defined]


@router.post("/positions/{position_id}/shortlists", operation_id="sendShortlist", status_code=status.HTTP_202_ACCEPTED)
async def send_shortlist(position_id: UUID, payload: ShortlistSend, record: ManagerPrincipal, idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> JSONResponse:
    from bluelab.adapters.email import validate_address
    scope = scope_of(record); body = payload.model_dump(mode="json")
    normalized = sorted({validate_address(str(item)).lower() for item in payload.recipients})
    domains = {item.rsplit("@", 1)[1] for item in normalized}
    key = require_key(idempotency_key, org_id=scope.org_id, principal_id=scope.account_id, endpoint=f"positions/{position_id}/shortlists")  # type: ignore[arg-type]
    async with scoped_transaction(scope) as db:
        await acquire_replay_lock(db, key=key); replay = await load_replay(db, key=key, body=body)
        if replay: return JSONResponse(replay.body, status_code=replay.status)
        historical = set((await db.execute(text("select distinct lower(split_part(value,'@',2)) from shortlist s cross join lateral jsonb_array_elements_text(s.recipients) value where s.sent_by=:owner"), {"owner": scope.account_id})).scalars().all())
        required_new = domains - historical
        if not payload.recipients_confirmed or set(payload.acknowledged_new_domains) != required_new:
            raise ValidationProblem([{"field":"recipients_confirmed", "message":"Confirm the resolved recipients and every new domain."}])
        shortlist_id = await shortlist.send_shortlist(db, position_id=position_id, org_id=scope.org_id, team_id=scope.team_id, sent_by=scope.account_id, candidate_ids=payload.candidate_ids, recipients=normalized, email_body=payload.body)  # type: ignore[arg-type]
        response = jsonable_encoder({"shortlist_id":shortlist_id,"included_candidate_ids":payload.candidate_ids})
        await store_response(db, key=key, body=body, status=202, response=response)
        return JSONResponse(response, status_code=202)
