"""T-10 — erasure and export (ADR-0033, data/03 §3-§4).

The dedicated procedures here are the **only sanctioned writers through the
freeze guards**. Erasure removes the person — identity fields, transcripts,
quotes, recordings, PDFs, export bundles — and leaves the statistical residue
(scores, times, statuses) standing. Neither procedure is reachable from a request
path; both are triggered from the ops surface and audited.
"""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.adapters.erasure_ledger import ErasureLedger, ErasureMarker
from bluelab.adapters.object_store import DeletionReason, ObjectRef, ObjectStore
from bluelab.platform.telemetry import metrics

SubjectKind = Literal["account", "candidate"]


class SubjectRequestUnavailable(RuntimeError):
    """The request identity cannot be executed by a worker."""


async def execute_erasure(
    session: AsyncSession,
    *,
    request_id: UUID,
    ledger: ErasureLedger,
    object_store: ObjectStore,
) -> None:
    """Arm both ledgers, run T-10, remove objects, and persist count evidence."""
    request = (
        await session.execute(
            text(
                "select id,org_id,subject_kind,subject_id,status,requested_at,executed_by "
                "from erasure_request where id=:id for update"
            ),
            {"id": request_id},
        )
    ).mappings().one_or_none()
    if request is None:
        raise SubjectRequestUnavailable("unknown erasure request")
    if request["status"] == "executed":
        return
    if request["status"] not in {"pending", "processing"}:
        raise SubjectRequestUnavailable("erasure request is not runnable")
    pending = await session.scalar(text(
        "select exists(select 1 from org_lifecycle_operation "
        "where org_id=:org and status='pending')"
    ), {"org": request["org_id"]})
    if pending:
        raise RuntimeError("lifecycle decision pending")
    if request["executed_by"] is None:
        raise SubjectRequestUnavailable("erasure request has no operator")

    marker = ErasureMarker(
        request_id=request_id,
        org_id=request["org_id"],
        subject_kind=request["subject_kind"],
        subject_id=request["subject_id"],
        requested_at=request["requested_at"],
        armed_at=datetime.now(UTC),
        executed_by=request["executed_by"],
    )
    await ledger.arm(marker)
    evidence = dict(
        (
            await session.execute(
                text("select app_execute_erasure(:id)"), {"id": request_id}
            )
        ).scalar_one()
    )
    keys = [str(value) for value in evidence.pop("object_keys", [])]
    deleted = 0
    for key in keys:
        ref = ObjectRef(key)
        await object_store.delete(ref, reason=DeletionReason.ERASURE)
        if await object_store.exists(ref):
            raise RuntimeError("erasure object remains")
        deleted += 1
    verification = dict(
        (
            await session.execute(
                text("select app_verify_erasure(:id)"), {"id": request_id}
            )
        ).scalar_one()
    )
    if any(int(value) for value in verification.values()):
        raise RuntimeError("erasure database verification failed")
    evidence["objects_deleted"] = deleted
    evidence["verified_at"] = datetime.now(UTC).isoformat()
    for category, value in evidence.items():
        if category != "verified_at":
            metrics.record_erasure_category(category=category, count=int(value))
    await session.execute(
        text(
            "update erasure_request set status='executed',executed_at=now(),"
            "evidence=cast(:evidence as jsonb) where id=:id"
        ),
        {
            "id": request_id,
            "evidence": json.dumps(evidence, separators=(",", ":"), sort_keys=True),
        },
    )


async def fail_erasure(session: AsyncSession, *, request_id: UUID) -> None:
    await session.execute(
        text(
            "update erasure_request set status='failed',"
            "evidence='{\"failure\":\"retry_exhausted\"}'::jsonb "
            "where id=:id and status in ('pending','processing')"
        ),
        {"id": request_id},
    )


async def execute_export(
    session: AsyncSession,
    *,
    request_id: UUID,
    object_store: ObjectStore,
) -> None:
    """Build one complete subject bundle under its fixed, expiring key."""
    request = (
        await session.execute(
            text("select * from export_request where id=:id for update"),
            {"id": request_id},
        )
    ).mappings().one_or_none()
    if request is None:
        raise SubjectRequestUnavailable("unknown export request")
    if request["status"] in {"ready", "delivered"}:
        return
    if request["status"] not in {"pending", "processing"}:
        raise SubjectRequestUnavailable("export request is not runnable")
    pending = await session.scalar(text(
        "select exists(select 1 from org_lifecycle_operation "
        "where org_id=:org and status='pending')"
    ), {"org": request["org_id"]})
    if pending:
        raise RuntimeError("lifecycle decision pending")

    subject_id: UUID = request["subject_id"]
    await session.execute(
        text("select pg_advisory_xact_lock(hashtextextended(:subject,0))"),
        {"subject": str(subject_id)},
    )
    erased = await session.scalar(
        text(
            "select exists(select 1 from erasure_request where org_id=:org "
            "and subject_kind=:kind and subject_id=:subject)"
        ),
        {
            "org": request["org_id"],
            "kind": request["subject_kind"],
            "subject": subject_id,
        },
    )
    if erased:
        await fail_export(session, request_id=request_id)
        return

    inventory, object_refs = await subject_inventory(
        session,
        org_id=request["org_id"],
        subject_kind=request["subject_kind"],
        subject_id=subject_id,
    )
    bundle = await _bundle(inventory, object_refs=object_refs, object_store=object_store)
    ref = ObjectRef.export(request_id=request_id)
    await object_store.put(ref, bundle, content_type="application/zip")
    await session.execute(
        text(
            "update export_request set status='ready',bundle_object_key=:key,"
            "ready_at=now(),expires_at=now()+interval '7 days' where id=:id"
        ),
        {"id": request_id, "key": ref.key},
    )


async def fail_export(session: AsyncSession, *, request_id: UUID) -> None:
    await session.execute(
        text(
            "update export_request set status='failed',bundle_object_key=null,"
            "expires_at=null where id=:id and status in ('pending','processing')"
        ),
        {"id": request_id},
    )


async def subject_inventory(
    session: AsyncSession, *, org_id: UUID, subject_kind: SubjectKind, subject_id: UUID
) -> tuple[dict[str, list[dict[str, Any]]], list[ObjectRef]]:
    """Return the category-complete export projection without credential secrets."""
    subject_table = "account" if subject_kind == "account" else "candidate"
    owns_subject = await session.scalar(
        text(f"select exists(select 1 from {subject_table} "
             "where id=:subject and org_id=:org)"),
        {"subject": subject_id, "org": org_id},
    )
    if not owns_subject:
        raise SubjectRequestUnavailable("subject is outside request organization")
    categories: dict[str, list[dict[str, Any]]] = {}

    async def rows(name: str, sql: str) -> None:
        result = (await session.execute(
            text(sql), {"subject": subject_id, "org": org_id}
        )).scalars()
        categories[name] = [dict(value) for value in result if value is not None]

    if subject_kind == "account":
        await rows(
            "identity",
            "select to_jsonb(a)-'password_hash' data from account a "
            "where id=:subject and org_id=:org",
        )
        participant = "a.rep_account_id=:subject"
        await rows(
            "feedback",
            "select to_jsonb(f) data from coach_feedback_item f where rep_account_id=:subject",
        )
        await rows(
            "badges",
            "select to_jsonb(b) data from badge_award b where account_id=:subject",
        )
        await rows(
            "assignment_memberships",
            "select to_jsonb(r) data from assignment_recipient r where rep_account_id=:subject",
        )
        await rows(
            "tokens",
            "select to_jsonb(t)-'token_hash' data from password_reset_token t where account_id=:subject",
        )
    else:
        await rows(
            "identity", "select to_jsonb(c) data from candidate c "
            "where id=:subject and org_id=:org",
        )
        participant = "a.candidate_id=:subject"
        await rows(
            "candidate_report",
            "select to_jsonb(r) data from candidate_report r where candidate_id=:subject",
        )
        await rows(
            "shortlists",
            "select to_jsonb(si) data from shortlist_candidate si where candidate_id=:subject",
        )

    await rows(
        "consent",
        "select to_jsonb(c) data from consent_record c where "
        f"{'account_id' if subject_kind == 'account' else 'candidate_id'}=:subject",
    )
    await rows(
        "terms",
        "select to_jsonb(t) data from terms_acceptance t where "
        f"{'account_id' if subject_kind == 'account' else 'candidate_id'}=:subject",
    )
    await rows("attempts", f"select to_jsonb(a) data from attempt a where {participant}")
    for name, table in (
        ("transcripts", "transcript_entry"),
        ("moments", "moment"),
    ):
        await rows(
            name,
            f"select to_jsonb(x) data from {table} x join attempt a on a.id=x.attempt_id "
            f"where {participant}",
        )
    await rows(
        "scorecards",
        "select to_jsonb(s) data from scorecard s join attempt a on a.id=s.attempt_id "
        f"where {participant}",
    )
    await rows(
        "dimension_scores",
        "select to_jsonb(d) data from dimension_score d join scorecard s on s.id=d.scorecard_id "
        "join attempt a on a.id=s.attempt_id " f"where {participant}",
    )
    await rows(
        "delivery_history",
        "select to_jsonb(e)-'provider_message_id' data from email_send e where "
        f"{'account_id' if subject_kind == 'account' else 'candidate_id'}=:subject",
    )
    if subject_kind == "candidate":
        await rows(
            "tokens",
            "select to_jsonb(t)-'token_hash' data from candidate_token t where candidate_id=:subject",
        )
    await rows(
        "erasure_requests",
        "select to_jsonb(r) data from erasure_request r "
        "where subject_id=:subject and org_id=:org",
    )
    await rows(
        "export_requests",
        "select to_jsonb(r)-'bundle_object_key' data from export_request r "
        "where subject_id=:subject and org_id=:org",
    )
    await rows(
        "ops_audit",
        "select to_jsonb(a) data from ops_audit a where a.target_org_id=:org "
        "and (a.target_ref @> "
        "jsonb_build_object('subject_id',cast(:subject as text)) or a.target_ref @> "
        "jsonb_build_object('account_id',cast(:subject as text)))",
    )

    object_keys = {
        row["recording_object_key"]
        for row in categories["attempts"]
        if row.get("recording_object_key")
    }
    if subject_kind == "candidate":
        object_keys.update(
            row["pdf_object_key"]
            for row in categories["candidate_report"]
            if row.get("pdf_object_key")
        )
    return categories, [ObjectRef(key) for key in sorted(object_keys)]


async def _bundle(
    inventory: dict[str, list[dict[str, Any]]],
    *,
    object_refs: Iterable[ObjectRef],
    object_store: ObjectStore,
) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "data.json",
            json.dumps(inventory, default=str, ensure_ascii=False, indent=2),
        )
        for index, ref in enumerate(object_refs, start=1):
            archive.writestr(
                f"objects/{index:04d}-{ref.key.rsplit('/', 1)[-1]}",
                await object_store.get(ref),
            )
    return stream.getvalue()


async def expire_exports(
    session: AsyncSession, *, object_store: ObjectStore, at: datetime | None = None
) -> int:
    """Delete expired bundles before clearing their authoritative references."""
    cutoff = at or datetime.now(UTC)
    rows = (
        await session.execute(
            text(
                "select id,bundle_object_key from export_request "
                "where expires_at<=:cutoff and bundle_object_key is not null for update skip locked"
            ),
            {"cutoff": cutoff},
        )
    ).mappings().all()
    for row in rows:
        await object_store.delete(ObjectRef(row["bundle_object_key"]), reason=DeletionReason.SWEEP)
        await session.execute(
            text(
                "update export_request set bundle_object_key=null where id=:id"
            ),
            {"id": row["id"]},
        )
    return len(rows)
