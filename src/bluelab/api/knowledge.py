"""Manager-only product knowledge HTTP surface (FR-KNW-001–011)."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, UploadFile, status

from bluelab.adapters.object_store import ObjectRef
from bluelab.api.deps import ManagerPrincipal, ObjectStoreDep, scope_of
from bluelab.modules.knowledge import service
from bluelab.modules.knowledge.models import MAX_UPLOAD_BYTES
from bluelab.modules.knowledge.schemas import (
    CreateDocument,
    DocumentDetail,
    DocumentList,
    DocumentSummary,
    ManualDraft,
    PublishDocument,
    ReviewDiff,
    UploadAccepted,
)
from bluelab.platform.db.session import scoped_transaction
from bluelab.platform.ids import new_id

router = APIRouter(tags=["Knowledge"])


@router.get(
    "/product-documents",
    operation_id="listProductDocuments",
    response_model=DocumentList,
)
async def list_product_documents(record: ManagerPrincipal) -> DocumentList:
    async with scoped_transaction(scope_of(record)) as db:
        data = await service.list_documents(
            db, org_id=UUID(record.org_id), team_id=UUID(record.team_id)
        )
    return DocumentList(data=data)


@router.post(
    "/product-documents",
    operation_id="createProductDocument",
    response_model=DocumentSummary,
    status_code=status.HTTP_201_CREATED,
)
async def create_product_document(
    payload: CreateDocument, record: ManagerPrincipal
) -> DocumentSummary:
    async with scoped_transaction(scope_of(record)) as db:
        return await service.create_document(
            db,
            org_id=UUID(record.org_id),
            team_id=UUID(record.team_id),
            title=payload.title,
        )


@router.get(
    "/product-documents/{document_id}",
    operation_id="getProductDocument",
    response_model=DocumentDetail,
)
async def get_product_document(
    document_id: UUID, record: ManagerPrincipal
) -> DocumentDetail:
    async with scoped_transaction(scope_of(record)) as db:
        return await service.get_document(
            db,
            document_id=document_id,
            org_id=UUID(record.org_id),
            team_id=UUID(record.team_id),
        )


@router.post(
    "/product-documents/{document_id}/uploads",
    operation_id="uploadDocumentSource",
    response_model=UploadAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_document_source(
    document_id: UUID,
    record: ManagerPrincipal,
    object_store: ObjectStoreDep,
    file: Annotated[UploadFile, File()],
) -> UploadAccepted:
    # Read one byte beyond the limit so refusal does not depend on Content-Length.
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    validated = service.validate_upload(file.filename, file.content_type, data)
    async with scoped_transaction(scope_of(record)) as db:
        await service.ensure_document_exists(
            db,
            document_id=document_id,
            org_id=UUID(record.org_id),
            team_id=UUID(record.team_id),
        )
    upload_id = new_id()
    ref = ObjectRef.upload(org_id=UUID(record.org_id), upload_id=upload_id)
    await object_store.put(ref, validated.data, content_type=validated.content_type)
    async with scoped_transaction(scope_of(record)) as db:
        await service.begin_upload(
            db,
            document_id=document_id,
            upload_id=upload_id,
            org_id=UUID(record.org_id),
            team_id=UUID(record.team_id),
            author_id=UUID(record.account_id),
            filename=validated.filename,
            byte_size=len(validated.data),
            object_key=ref.key,
        )
    return UploadAccepted(upload_id=upload_id)


@router.put(
    "/product-documents/{document_id}/draft",
    operation_id="putManualDraft",
    response_model=DocumentDetail,
)
async def put_manual_draft(
    document_id: UUID, payload: ManualDraft, record: ManagerPrincipal
) -> DocumentDetail:
    async with scoped_transaction(scope_of(record)) as db:
        await service.replace_manual_draft(
            db,
            document_id=document_id,
            org_id=UUID(record.org_id),
            team_id=UUID(record.team_id),
            author_id=UUID(record.account_id),
            facts=payload.facts,
        )
    async with scoped_transaction(scope_of(record)) as db:
        return await service.get_document(
            db,
            document_id=document_id,
            org_id=UUID(record.org_id),
            team_id=UUID(record.team_id),
        )


@router.get(
    "/product-documents/{document_id}/review",
    operation_id="getDocumentReview",
    response_model=ReviewDiff,
)
async def get_document_review(
    document_id: UUID, record: ManagerPrincipal
) -> ReviewDiff:
    async with scoped_transaction(scope_of(record)) as db:
        return await service.get_review(
            db,
            document_id=document_id,
            org_id=UUID(record.org_id),
            team_id=UUID(record.team_id),
        )


@router.post(
    "/product-documents/{document_id}/publish",
    operation_id="publishDocument",
    response_model=DocumentDetail,
)
async def publish_document(
    document_id: UUID, payload: PublishDocument, record: ManagerPrincipal
) -> DocumentDetail:
    async with scoped_transaction(scope_of(record)) as db:
        await service.publish_review(
            db,
            document_id=document_id,
            org_id=UUID(record.org_id),
            team_id=UUID(record.team_id),
            publisher_id=UUID(record.account_id),
            based_on_version=payload.based_on_version,
        )
    async with scoped_transaction(scope_of(record)) as db:
        return await service.get_document(
            db,
            document_id=document_id,
            org_id=UUID(record.org_id),
            team_id=UUID(record.team_id),
        )
