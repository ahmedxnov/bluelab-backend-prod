"""Knowledge lifecycle and safe upload validation (FR-KNW-001–011)."""

from __future__ import annotations

import io
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.modules.knowledge import publish
from bluelab.modules.knowledge.models import MAX_UPLOAD_BYTES
from bluelab.modules.knowledge.schemas import (
    DocumentDetail,
    DocumentSummary,
    FactInput,
    ReviewDiff,
    ReviewEntry,
)
from bluelab.platform.errors import catalog
from bluelab.platform.errors.denial import ProblemError, not_found
from bluelab.platform.ids import new_id
from bluelab.platform.queue.catalog import Lane
from bluelab.platform.queue.enqueue import enqueue

DOCUMENT_CAP = 10
SUPPORTED_UPLOADS = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
_OPENXML_ROOT = {".docx": "word/", ".xlsx": "xl/", ".pptx": "ppt/"}


@dataclass(frozen=True, slots=True)
class ValidatedUpload:
    filename: str
    content_type: str
    data: bytes


def validate_upload(
    filename: str | None, content_type: str | None, data: bytes
) -> ValidatedUpload:
    """Validate size, extension, signature and bounded OpenXML structure (SEC-017)."""
    problem_meta: dict[str, object] = {"supported": sorted(SUPPORTED_UPLOADS)}
    if len(data) > MAX_UPLOAD_BYTES:
        raise ProblemError(catalog.FILE_TOO_LARGE, meta={"max_bytes": MAX_UPLOAD_BYTES})
    if not data:
        raise ProblemError(catalog.UNSUPPORTED_FILE_TYPE, meta=problem_meta)
    safe_name = PurePath(filename or "").name
    suffix = PurePath(safe_name).suffix.lower()
    expected = SUPPORTED_UPLOADS.get(suffix)
    if expected is None or content_type not in {expected, "application/octet-stream"}:
        raise ProblemError(catalog.UNSUPPORTED_FILE_TYPE, meta=problem_meta)
    if suffix == ".pdf":
        if not data.startswith(b"%PDF-"):
            raise ProblemError(catalog.UNSUPPORTED_FILE_TYPE, meta=problem_meta)
    else:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                members = archive.infolist()
                names = [item.filename.replace("\\", "/").lower() for item in members]
                total = sum(item.file_size for item in members)
                invalid = (
                    len(members) > 1000
                    or total > 100 * 1024 * 1024
                    or "[content_types].xml" not in names
                    or not any(name.startswith(_OPENXML_ROOT[suffix]) for name in names)
                    or any(
                        "vbaproject.bin" in name or name.endswith(".exe")
                        for name in names
                    )
                    or any(".." in PurePath(name).parts for name in names)
                    or any(
                        item.compress_size > 0
                        and item.file_size / item.compress_size > 100
                        for item in members
                    )
                )
                if invalid:
                    raise zipfile.BadZipFile
        except (zipfile.BadZipFile, OSError, ValueError):
            raise ProblemError(
                catalog.UNSUPPORTED_FILE_TYPE, meta=problem_meta
            ) from None
    return ValidatedUpload(filename=safe_name, content_type=expected, data=data)


async def _document_rows(
    session: AsyncSession, *, org_id: UUID, team_id: UUID
) -> list[Any]:
    return list(
        (
            await session.execute(
                text(
                    """
select d.id,d.title,d.live_version,d.updated_at,
       coalesce((select count(*) from product_fact f join fact_set s on s.id=f.fact_set_id
                 where s.document_id=d.id and s.kind='live'),0) live_fact_count,
       ds.source draft_source,ds.created_at draft_created_at,
       case when ds.id is not null then 'pending_review'
            when u.status in ('received','extracting') then 'extracting'
            when u.status='failed' then 'failed' end draft_status,
       u.failure_reason,u.created_at upload_created_at
from product_document d
left join lateral (select * from fact_set where document_id=d.id and kind='draft' limit 1) ds on true
left join lateral (select * from document_upload where document_id=d.id
                   order by created_at desc,id desc limit 1) u on true
where d.org_id=:org and d.team_id=:team
order by d.updated_at desc,d.id desc
                    """
                ),
                {"org": org_id, "team": team_id},
            )
        ).mappings()
    )


def _summary(row: Any) -> DocumentSummary:
    marker = None
    if row["draft_status"] is not None:
        marker = {
            "source": row["draft_source"] or "upload",
            "status": row["draft_status"],
            "failure_reason": (
                row["failure_reason"] if row["draft_status"] == "failed" else None
            ),
            "created_at": row["draft_created_at"] or row["upload_created_at"],
        }
    return DocumentSummary.model_validate(
        {
            "id": row["id"],
            "title": row["title"],
            "live_fact_count": row["live_fact_count"],
            "live_version": row["live_version"],
            "draft": marker,
            "updated_at": row["updated_at"],
        }
    )


async def list_documents(
    session: AsyncSession, *, org_id: UUID, team_id: UUID
) -> list[DocumentSummary]:
    return [
        _summary(row)
        for row in await _document_rows(session, org_id=org_id, team_id=team_id)
    ]


async def create_document(
    session: AsyncSession, *, org_id: UUID, team_id: UUID, title: str
) -> DocumentSummary:
    await session.execute(
        text("select pg_advisory_xact_lock(hashtextextended(cast(:team as text),0))"),
        {"team": str(team_id)},
    )
    count = (
        await session.execute(
            text(
                "select count(*) from product_document where org_id=:org and team_id=:team"
            ),
            {"org": org_id, "team": team_id},
        )
    ).scalar_one()
    if count >= DOCUMENT_CAP:
        raise ProblemError(catalog.DOCUMENT_CAP)
    row = (
        (
            await session.execute(
                text(
                    "insert into product_document(id,org_id,team_id,title) values(:id,:org,:team,:title) returning id,title,live_version,updated_at"
                ),
                {"id": new_id(), "org": org_id, "team": team_id, "title": title},
            )
        )
        .mappings()
        .one()
    )
    return DocumentSummary(**row, live_fact_count=0, draft=None)


async def get_document(
    session: AsyncSession, *, document_id: UUID, org_id: UUID, team_id: UUID
) -> DocumentDetail:
    rows = await _document_rows(session, org_id=org_id, team_id=team_id)
    raw = next((row for row in rows if row["id"] == document_id), None)
    if raw is None:
        raise not_found()
    facts = (
        (
            await session.execute(
                text(
                    "select f.id,f.ord,f.label,f.value,f.note from product_fact f join fact_set s on s.id=f.fact_set_id where s.document_id=:document and s.kind='live' and f.org_id=:org and f.team_id=:team order by f.ord,f.id"
                ),
                {"document": document_id, "org": org_id, "team": team_id},
            )
        )
        .mappings()
        .all()
    )
    upload = (
        (
            await session.execute(
                text(
                    "select id upload_id,filename,status,failure_reason,created_at from document_upload where document_id=:document and org_id=:org and team_id=:team order by created_at desc,id desc limit 1"
                ),
                {"document": document_id, "org": org_id, "team": team_id},
            )
        )
        .mappings()
        .first()
    )
    return DocumentDetail.model_validate(
        {
            **_summary(raw).model_dump(),
            "live_facts": list(facts),
            "latest_upload": dict(upload) if upload is not None else None,
        }
    )


async def replace_manual_draft(
    session: AsyncSession,
    *,
    document_id: UUID,
    org_id: UUID,
    team_id: UUID,
    author_id: UUID,
    facts: Sequence[FactInput],
) -> None:
    version = (
        await session.execute(
            text(
                "select live_version from product_document where id=:document and org_id=:org and team_id=:team for update"
            ),
            {"document": document_id, "org": org_id, "team": team_id},
        )
    ).scalar_one_or_none()
    if version is None:
        raise not_found()
    await session.execute(
        text("delete from fact_set where document_id=:document and kind='draft'"),
        {"document": document_id},
    )
    draft_id = new_id()
    await session.execute(
        text(
            "insert into fact_set(id,org_id,team_id,document_id,kind,source,based_on_version,created_by) values(:id,:org,:team,:document,'draft','manual',:version,:author)"
        ),
        {
            "id": draft_id,
            "org": org_id,
            "team": team_id,
            "document": document_id,
            "version": version,
            "author": author_id,
        },
    )
    live = (
        (
            await session.execute(
                text(
                    "select f.id,f.label from product_fact f join fact_set s on s.id=f.fact_set_id where s.document_id=:document and s.kind='live'"
                ),
                {"document": document_id},
            )
        )
        .mappings()
        .all()
    )
    by_label = {row["label"]: row["id"] for row in live}
    for ordinal, fact in enumerate(facts, 1):
        await session.execute(
            text(
                "insert into product_fact(id,org_id,team_id,fact_set_id,ord,label,value,note,prior_fact_id) values(:id,:org,:team,:set,:ord,:label,:value,:note,:prior)"
            ),
            {
                "id": new_id(),
                "org": org_id,
                "team": team_id,
                "set": draft_id,
                "ord": ordinal,
                "label": fact.label,
                "value": fact.value,
                "note": fact.note,
                "prior": by_label.get(fact.label),
            },
        )


async def begin_upload(
    session: AsyncSession,
    *,
    document_id: UUID,
    upload_id: UUID,
    org_id: UUID,
    team_id: UUID,
    author_id: UUID,
    filename: str,
    byte_size: int,
    object_key: str,
) -> None:
    exists = (
        await session.execute(
            text(
                "select 1 from product_document where id=:document and org_id=:org and team_id=:team for update"
            ),
            {"document": document_id, "org": org_id, "team": team_id},
        )
    ).scalar_one_or_none()
    if exists is None:
        raise not_found()
    await session.execute(
        text("delete from fact_set where document_id=:document and kind='draft'"),
        {"document": document_id},
    )
    await session.execute(
        text(
            "insert into document_upload(id,org_id,team_id,document_id,object_key,filename,byte_size,created_by) values(:id,:org,:team,:document,:key,:filename,:size,:author)"
        ),
        {
            "id": upload_id,
            "org": org_id,
            "team": team_id,
            "document": document_id,
            "key": object_key,
            "filename": filename,
            "size": byte_size,
            "author": author_id,
        },
    )
    await enqueue(
        session,
        Lane.EXTRACT_FACTS,
        {"upload_id": str(upload_id)},
        org_id=org_id,
        team_id=team_id,
    )


async def ensure_document_exists(
    session: AsyncSession, *, document_id: UUID, org_id: UUID, team_id: UUID
) -> None:
    """Authorize an upload target before storing its object bytes."""
    exists = (
        await session.execute(
            text(
                "select 1 from product_document where id=:document and org_id=:org and team_id=:team"
            ),
            {"document": document_id, "org": org_id, "team": team_id},
        )
    ).scalar_one_or_none()
    if exists is None:
        raise not_found()


async def get_review(
    session: AsyncSession, *, document_id: UUID, org_id: UUID, team_id: UUID
) -> ReviewDiff:
    version = (
        await session.execute(
            text(
                "select live_version from product_document where id=:document and org_id=:org and team_id=:team"
            ),
            {"document": document_id, "org": org_id, "team": team_id},
        )
    ).scalar_one_or_none()
    if version is None:
        raise not_found()
    draft = (
        await session.execute(
            text(
                "select id from fact_set where document_id=:document and kind='draft'"
            ),
            {"document": document_id},
        )
    ).scalar_one_or_none()
    if draft is None:
        state = (
            await session.execute(
                text(
                    "select status from document_upload where document_id=:document order by created_at desc,id desc limit 1"
                ),
                {"document": document_id},
            )
        ).scalar_one_or_none()
        if state in ("received", "extracting"):
            raise ProblemError(catalog.EXTRACTION_PENDING)
        raise ProblemError(catalog.NO_DRAFT_TO_REVIEW)
    live_rows = (
        (
            await session.execute(
                text(
                    "select f.label,f.value,f.note from product_fact f join fact_set s on s.id=f.fact_set_id where s.document_id=:document and s.kind='live' order by f.ord,f.id"
                ),
                {"document": document_id},
            )
        )
        .mappings()
        .all()
    )
    draft_rows = (
        (
            await session.execute(
                text(
                    "select label,value,note from product_fact where fact_set_id=:draft order by ord,id"
                ),
                {"draft": draft},
            )
        )
        .mappings()
        .all()
    )
    live = {row["label"]: row for row in live_rows}
    seen: set[str] = set()
    entries: list[ReviewEntry] = []
    for row in draft_rows:
        seen.add(row["label"])
        prior = live.get(row["label"])
        entry_status = (
            "added"
            if prior is None
            else (
                "unchanged"
                if row["value"] == prior["value"] and row["note"] == prior["note"]
                else "changed"
            )
        )
        entries.append(
            ReviewEntry.model_validate(
                {
                    "status": entry_status,
                    "label": row["label"],
                    "value": row["value"],
                    "note": row["note"],
                    "prior": dict(prior)
                    if prior is not None and entry_status == "changed"
                    else None,
                }
            )
        )
    for row in live_rows:
        if row["label"] not in seen:
            entries.append(
                ReviewEntry.model_validate(
                    {
                        "status": "removed",
                        "label": row["label"],
                        "value": row["value"],
                        "note": row["note"],
                        "prior": dict(row),
                    }
                )
            )
    return ReviewDiff(
        document_id=document_id,
        based_on_version=int(version),
        warning="Publishing replaces all current facts for this document.",
        entries=entries,
    )


async def publish_review(
    session: AsyncSession,
    *,
    document_id: UUID,
    org_id: UUID,
    team_id: UUID,
    publisher_id: UUID,
    based_on_version: int,
) -> None:
    await publish.publish_facts(
        session,
        document_id=document_id,
        org_id=org_id,
        team_id=team_id,
        publisher_account_id=publisher_id,
        based_on_version=based_on_version,
    )


async def extraction_basis(
    session: AsyncSession, *, upload_id: UUID
) -> dict[str, Any] | None:
    row = (
        (
            await session.execute(
                text(
                    "update document_upload set status='extracting',failure_reason=null where id=:upload and status='received' returning id,org_id,team_id,document_id,object_key,filename,created_by"
                ),
                {"upload": upload_id},
            )
        )
        .mappings()
        .first()
    )
    if row is not None:
        return dict(row)
    # At-least-once delivery of a terminal upload is an intentional no-op.
    return None


async def apply_extracted_facts(
    session: AsyncSession, *, upload_id: UUID, facts: Sequence[FactInput]
) -> bool:
    upload = (
        (
            await session.execute(
                text(
                    "select * from document_upload where id=:upload and status='extracting' for update"
                ),
                {"upload": upload_id},
            )
        )
        .mappings()
        .first()
    )
    if upload is None:
        return False
    latest = (
        await session.execute(
            text(
                "select id from document_upload where document_id=:document order by created_at desc,id desc limit 1"
            ),
            {"document": upload["document_id"]},
        )
    ).scalar_one()
    if latest != upload_id:
        await session.execute(
            text("update document_upload set status='extracted' where id=:upload"),
            {"upload": upload_id},
        )
        return False
    version = (
        await session.execute(
            text(
                "select live_version from product_document where id=:document for update"
            ),
            {"document": upload["document_id"]},
        )
    ).scalar_one()
    await session.execute(
        text("delete from fact_set where document_id=:document and kind='draft'"),
        {"document": upload["document_id"]},
    )
    draft_id = new_id()
    await session.execute(
        text(
            "insert into fact_set(id,org_id,team_id,document_id,kind,source,based_on_version,created_by) values(:id,:org,:team,:document,'draft','upload',:version,:author)"
        ),
        {
            "id": draft_id,
            "org": upload["org_id"],
            "team": upload["team_id"],
            "document": upload["document_id"],
            "version": version,
            "author": upload["created_by"],
        },
    )
    live = (
        (
            await session.execute(
                text(
                    "select f.id,f.label from product_fact f join fact_set s on s.id=f.fact_set_id where s.document_id=:document and s.kind='live'"
                ),
                {"document": upload["document_id"]},
            )
        )
        .mappings()
        .all()
    )
    by_label = {row["label"]: row["id"] for row in live}
    for ordinal, fact in enumerate(facts, 1):
        await session.execute(
            text(
                "insert into product_fact(id,org_id,team_id,fact_set_id,ord,label,value,note,prior_fact_id) values(:id,:org,:team,:set,:ord,:label,:value,:note,:prior)"
            ),
            {
                "id": new_id(),
                "org": upload["org_id"],
                "team": upload["team_id"],
                "set": draft_id,
                "ord": ordinal,
                "label": fact.label,
                "value": fact.value,
                "note": fact.note,
                "prior": by_label.get(fact.label),
            },
        )
    await session.execute(
        text(
            "update document_upload set status='extracted',failure_reason=null where id=:upload and status='extracting'"
        ),
        {"upload": upload_id},
    )
    return True


async def mark_extraction_failed(session: AsyncSession, *, upload_id: UUID) -> None:
    await session.execute(
        text(
            "update document_upload set status='failed',failure_reason='Extraction failed. Upload a supported file to retry.' where id=:upload and status in ('received','extracting')"
        ),
        {"upload": upload_id},
    )
