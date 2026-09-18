"""Ordered organization-purge claim and bounded-step orchestration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.adapters.lifecycle_history import (
    HistoryConflict,
    HistoryEvent,
    LifecycleHistory,
)
from bluelab.adapters.object_store import DeletionReason, ObjectRef, RestoreObjectStore
from bluelab.lifecycle.purge_matrix import (
    STEP_ORDER,
    STEP_TABLES,
    batch_keys,
    inventory_jobs,
    inventory_objects,
    inventory_rows,
)
from bluelab.modules.identity.models import Org
from bluelab.modules.operations.models import (
    ErasureRequest,
    ExportRequest,
    OrgDeletionRestriction,
    OrgLifecycleOperation,
    OrgPurgeRun,
    OrgPurgeStep,
)
from bluelab.platform.db.privileged import system_scope
from bluelab.platform.db.session import scoped_transaction
from bluelab.platform.security.sessions import SessionStore

CallQuiescence = Callable[[UUID, datetime], Awaitable[None]]
CLAIM_NAMESPACE = "bluelab:org-purge:claim"
RUN_NAMESPACE = "bluelab:org-purge:run"


class PurgeBlocked(RuntimeError):
    """The current episode is not authorized for destructive work."""


def claim_ids(org_id: UUID, offboarding_id: UUID) -> tuple[UUID, UUID]:
    """Bind retry identity to the episode rather than the current org row."""
    key = f"{org_id}:{offboarding_id}"
    return (
        uuid5(NAMESPACE_URL, f"{CLAIM_NAMESPACE}:{key}"),
        uuid5(NAMESPACE_URL, f"{RUN_NAMESPACE}:{key}"),
    )


def step_ids(run_id: UUID, step_key: str, batch_key: str) -> tuple[UUID, UUID, UUID]:
    """Stable step, authorization, and completion identities."""
    stem = f"bluelab:org-purge:{run_id}:{step_key}:{batch_key}"
    return (
        uuid5(NAMESPACE_URL, stem),
        uuid5(NAMESPACE_URL, stem + ":authorize"),
        uuid5(NAMESPACE_URL, stem + ":complete"),
    )


async def prepare_step_authorization(
    session: AsyncSession, *, run_id: UUID, step_key: str, batch_key: str,
    target_table: str | None, manifest_key: str, manifest_digest: str,
    expected_sequence: int,
) -> OrgLifecycleOperation:
    """Serialize one bounded authorization with restriction acceptance."""
    step_id, operation_id, _ = step_ids(run_id, step_key, batch_key)
    existing = await session.get(OrgLifecycleOperation, operation_id)
    if existing is not None:
        return existing
    run = (await session.execute(select(OrgPurgeRun).where(
        OrgPurgeRun.id == run_id,
    ).with_for_update())).scalar_one()
    org = (await session.execute(select(Org).where(
        Org.id == run.org_id,
    ).with_for_update())).scalar_one_or_none()
    if (org is None or org.lifecycle_status != "purging"
            or org.offboarding_id != run.offboarding_id
            or org.lifecycle_sequence != expected_sequence
            or run.status != "running" or run.execution_epoch < 1):
        raise PurgeBlocked("purge run is not authorized at this epoch")
    if await _claim_blocker(session, org.id):
        raise PurgeBlocked("deletion restriction or subject obligation remains")
    if (await session.scalar(select(OrgLifecycleOperation.id).where(
        OrgLifecycleOperation.org_id == run.org_id,
        OrgLifecycleOperation.status == "pending",
    ).limit(1))) is not None:
        raise PurgeBlocked("lifecycle decision is pending")
    payload: dict[str, Any] = {
        "run_id": str(run_id), "step_id": str(step_id),
        "step_key": step_key, "batch_key": batch_key,
        "target_table": target_table,
        "manifest_key": manifest_key, "manifest_digest": manifest_digest,
        "execution_epoch": run.execution_epoch,
    }
    operation = OrgLifecycleOperation(
        id=operation_id, org_id=org.id,
        service_term_id=run.service_term_id,
        offboarding_id=run.offboarding_id,
        action="authorize_destructive_step",
        expected_sequence=expected_sequence,
        actor_ops_account_id=None, reason="bounded purge batch",
        retention_policy_reference=run.retention_policy_reference,
        command_payload=payload,
    )
    session.add(operation)
    await session.flush()
    return operation


async def apply_step_authorization(
    session: AsyncSession, *, operation_id: UUID, event: HistoryEvent,
    manifest: dict[str, Any],
) -> OrgPurgeStep:
    """Project the independent authorization and exact batch key set."""
    operation = (await session.execute(select(OrgLifecycleOperation).where(
        OrgLifecycleOperation.id == operation_id,
    ).with_for_update())).scalar_one()
    data = operation.command_payload
    step_id = UUID(data["step_id"])
    if operation.status == "applied":
        step = await session.get(OrgPurgeStep, step_id)
        if step is None:
            raise PurgeBlocked("applied authorization has no step")
        return step
    if (operation.status != "pending" or operation.action != "authorize_destructive_step"
            or event.operation_id != operation.id or event.org_id != operation.org_id
            or event.action != operation.action or event.data != data
            or event.sequence != operation.expected_sequence + 1
            or manifest.get("step_key") != data["step_key"]
            or manifest.get("batch_key") != data["batch_key"]
            or manifest.get("target_table") != data["target_table"]
            or not isinstance(manifest.get("keys"), list)):
        raise PurgeBlocked("purge authorization or manifest mismatch")
    bound = 100 if data["step_key"] == "objects" or data["target_table"] == "sessions" else 1000
    if not 1 <= len(manifest["keys"]) <= bound:
        raise PurgeBlocked("purge batch exceeds its bound")
    run = (await session.execute(select(OrgPurgeRun).where(
        OrgPurgeRun.id == UUID(data["run_id"]),
    ).with_for_update())).scalar_one()
    org = (await session.execute(select(Org).where(
        Org.id == run.org_id,
    ).with_for_update())).scalar_one_or_none()
    if (org is None or org.lifecycle_status != "purging"
            or org.offboarding_id != run.offboarding_id
            or org.lifecycle_sequence != operation.expected_sequence
            or run.execution_epoch != data["execution_epoch"]):
        raise PurgeBlocked("purge authorization projection is stale")
    step = OrgPurgeStep(
        id=step_id, purge_run_id=run.id,
        step_key=data["step_key"], batch_key=data["batch_key"],
        batch_manifest_key=data["manifest_key"],
        batch_manifest_digest=data["manifest_digest"],
        target_table=data["target_table"], batch_keys=manifest["keys"],
        status="authorized", execution_epoch=run.execution_epoch,
        authorized_at=datetime.fromisoformat(event.accepted_at),
    )
    session.add(step)
    org.lifecycle_sequence = event.sequence
    operation.status = "applied"
    operation.resulting_sequence = event.sequence
    operation.resolved_at = datetime.now(UTC)
    await session.flush()
    return step


async def _claim_blocker(session: AsyncSession, org_id: UUID) -> str | None:
    if (await session.scalar(select(OrgDeletionRestriction.id).where(
        OrgDeletionRestriction.org_id == org_id,
        OrgDeletionRestriction.status == "active",
    ).limit(1))) is not None:
        return "active restriction"
    if (await session.scalar(select(ErasureRequest.id).where(
        ErasureRequest.org_id == org_id,
        ErasureRequest.status.in_(("pending", "processing", "awaiting_input", "failed")),
    ).limit(1))) is not None:
        return "unresolved erasure request"
    if (await session.scalar(select(ExportRequest.id).where(
        ExportRequest.org_id == org_id,
        ExportRequest.status.in_(("pending", "processing", "awaiting_input", "ready", "failed")),
    ).limit(1))) is not None:
        return "unresolved export request"
    return None


async def prepare_claim(
    session: AsyncSession, *, org_id: UUID, offboarding_id: UUID,
    expected_sequence: int, at: datetime,
) -> OrgLifecycleOperation:
    """Persist the claim fence after rechecking deadline and obligations."""
    operation_id, run_id = claim_ids(org_id, offboarding_id)
    existing = await session.get(OrgLifecycleOperation, operation_id)
    if existing is not None:
        if existing.org_id != org_id or existing.offboarding_id != offboarding_id:
            raise PurgeBlocked("claim identity is inconsistent")
        return existing
    org = (await session.execute(select(Org).where(Org.id == org_id).with_for_update()))\
        .scalar_one_or_none()
    if (org is None or org.lifecycle_status != "offboarding"
            or org.offboarding_id != offboarding_id
            or org.lifecycle_sequence != expected_sequence
            or org.purge_eligible_at is None or org.purge_eligible_at > at
            or org.offboarding_started_at is None
            or not org.retention_policy_reference):
        raise PurgeBlocked("episode, deadline, policy, or sequence is not eligible")
    blocker = await _claim_blocker(session, org_id)
    if blocker is not None:
        raise PurgeBlocked(blocker)
    pending = await session.scalar(select(OrgLifecycleOperation.id).where(
        OrgLifecycleOperation.org_id == org_id,
        OrgLifecycleOperation.status == "pending",
    ).limit(1))
    if pending is not None:
        raise PurgeBlocked("lifecycle decision is pending")
    payload: dict[str, Any] = {
        "run_id": str(run_id),
        "offboarding_id": str(offboarding_id),
        "service_term_id": str(org.current_service_term_id)
        if org.current_service_term_id else None,
        "hold_started_at": org.offboarding_started_at.isoformat(),
        "purge_eligible_at": org.purge_eligible_at.isoformat(),
        "retention_policy_reference": org.retention_policy_reference,
    }
    operation = OrgLifecycleOperation(
        id=operation_id, org_id=org_id,
        service_term_id=org.current_service_term_id,
        offboarding_id=offboarding_id, action="claim",
        expected_sequence=expected_sequence, actor_ops_account_id=None,
        reason="retention deadline reached",
        retention_policy_reference=org.retention_policy_reference,
        requested_deadline=org.purge_eligible_at,
        command_payload=payload,
    )
    session.add(operation)
    await session.flush()
    return operation


async def apply_claim(
    session: AsyncSession, *, operation_id: UUID, event: HistoryEvent,
) -> OrgPurgeRun:
    """Project the durable claim and its one run atomically."""
    operation = (await session.execute(select(OrgLifecycleOperation).where(
        OrgLifecycleOperation.id == operation_id,
    ).with_for_update())).scalar_one()
    run_id = UUID(operation.command_payload["run_id"])
    if operation.status == "applied":
        run = await session.get(OrgPurgeRun, run_id)
        if run is None:
            raise PurgeBlocked("applied claim has no run")
        return run
    if (operation.status != "pending" or operation.action != "claim"
            or event.operation_id != operation.id or event.org_id != operation.org_id
            or event.action != "claim" or event.sequence != operation.expected_sequence + 1
            or event.data != operation.command_payload):
        raise PurgeBlocked("claim event does not match its pending fence")
    org = (await session.execute(select(Org).where(
        Org.id == operation.org_id,
    ).with_for_update())).scalar_one_or_none()
    if (org is None or org.lifecycle_status != "offboarding"
            or org.offboarding_id != operation.offboarding_id
            or org.lifecycle_sequence != operation.expected_sequence):
        raise PurgeBlocked("claim projection cannot match current episode")
    if await _claim_blocker(session, org.id):
        raise PurgeBlocked("obligation changed before claim projection")
    org.lifecycle_status = "purging"
    org.lifecycle_sequence = event.sequence
    run = OrgPurgeRun(
        id=run_id, org_id=org.id, offboarding_id=operation.offboarding_id,
        service_term_id=operation.service_term_id,
        initiating_operator_id=org.offboarding_started_by,
        offboarding_started_at=org.offboarding_started_at,
        purge_eligible_at=org.purge_eligible_at,
        retention_policy_reference=org.retention_policy_reference,
        status="pending", execution_epoch=0,
    )
    session.add(run)
    operation.status = "applied"
    operation.resulting_sequence = event.sequence
    operation.resolved_at = datetime.now(UTC)
    await session.flush()
    return run


async def claim_one(
    org_id: UUID, offboarding_id: UUID, *, history: LifecycleHistory,
    quiesce: CallQuiescence, at: datetime | None = None,
) -> OrgPurgeRun:
    """Claim only after both the persisted fence and provider quiescence hold."""
    now = at or datetime.now(UTC)
    head = await history.verified_head(org_id)
    async with scoped_transaction(system_scope(org_id=org_id)) as db:
        operation = await prepare_claim(
            db, org_id=org_id, offboarding_id=offboarding_id,
            expected_sequence=head.sequence, at=now,
        )
    if operation.status == "applied":
        async with scoped_transaction(system_scope(org_id=org_id)) as db:
            run = await db.get(OrgPurgeRun, UUID(operation.command_payload["run_id"]))
            if run is None:
                raise PurgeBlocked("applied claim has no run")
            return run
    await quiesce(org_id, datetime.fromisoformat(
        operation.command_payload["hold_started_at"],
    ))
    try:
        event = await history.append(
            org_id=org_id, operation_id=operation.id,
            expected_sequence=operation.expected_sequence,
            action="claim", accepted_at=operation.created_at.isoformat(),
            actor_id=None, reason=operation.reason,
            data=operation.command_payload,
        )
    except HistoryConflict:
        accepted = await history.accepted(org_id, operation.id)
        if accepted is None:
            raise PurgeBlocked("claim lost lifecycle sequence race") from None
        event = accepted
    async with scoped_transaction(system_scope(org_id=org_id)) as db:
        return await apply_claim(db, operation_id=operation.id, event=event)


async def authorize_step(
    *, org_id: UUID, run_id: UUID, step_key: str, batch_key: str,
    target_table: str | None, keys: list[dict[str, str]],
    history: LifecycleHistory,
) -> OrgPurgeStep:
    """Pin a key manifest and append authorization before any side effect."""
    if not keys or len(keys) > (100 if step_key == "objects" or target_table == "sessions" else 1000):
        raise PurgeBlocked("invalid purge batch size")
    async with scoped_transaction(system_scope(org_id=org_id)) as db:
        run = await db.get(OrgPurgeRun, run_id)
        if run is None or run.status != "running":
            raise PurgeBlocked("purge run is not executing")
        batch_key = f"{batch_key}:e{run.execution_epoch}"
    manifest = {
        "rule_version": "org-purge:v1", "org_id": str(org_id),
        "run_id": str(run_id), "step_key": step_key,
        "batch_key": batch_key, "target_table": target_table,
        "keys": keys,
    }
    safe_batch_key = batch_key.replace(":", "_")
    manifest_key = f"org-purge/{org_id}/{run_id}/batches/{step_key}/{safe_batch_key}.json"
    digest = await history.put_purge_record(manifest_key, manifest)
    head = await history.verified_head(org_id)
    async with scoped_transaction(system_scope(org_id=org_id)) as db:
        operation = await prepare_step_authorization(
            db, run_id=run_id, step_key=step_key, batch_key=batch_key,
            target_table=target_table, manifest_key=manifest_key,
            manifest_digest=digest, expected_sequence=head.sequence,
        )
    event = await history.append(
        org_id=org_id, operation_id=operation.id,
        expected_sequence=operation.expected_sequence,
        action=operation.action, accepted_at=operation.created_at.isoformat(),
        actor_id=None, reason=operation.reason,
        data=operation.command_payload,
    )
    verified_manifest = await history.read_purge_record(manifest_key, digest)
    async with scoped_transaction(system_scope(org_id=org_id)) as db:
        return await apply_step_authorization(
            db, operation_id=operation.id, event=event,
            manifest=verified_manifest,
        )


async def execute_step(
    *, org_id: UUID, step: OrgPurgeStep, object_store: RestoreObjectStore,
    history: LifecycleHistory, sessions: SessionStore,
) -> int:
    """Execute one already-authorized batch with epoch fencing at its side effect."""
    if step.status == "completed":
        return step.deleted_count
    manifest = await history.read_purge_record(
        step.batch_manifest_key, step.batch_manifest_digest,
    )
    if (manifest.get("org_id") != str(org_id)
            or manifest.get("run_id") != str(step.purge_run_id)
            or manifest.get("keys") != step.batch_keys):
        raise PurgeBlocked("purge batch manifest disagrees with its projection")
    async with scoped_transaction(system_scope(org_id=org_id)) as db:
        run = (await db.execute(select(OrgPurgeRun).where(
            OrgPurgeRun.id == step.purge_run_id,
        ).with_for_update())).scalar_one()
        current = (await db.execute(select(OrgPurgeStep).where(
            OrgPurgeStep.id == step.id,
        ).with_for_update())).scalar_one()
        if (current.status not in {"authorized", "failed"}
                or current.execution_epoch != run.execution_epoch
                or run.status not in {"running", "retry_pending", "paused_restriction"}):
            raise PurgeBlocked("purge worker lost its execution epoch")
        if current.step_key == "objects":
            if len(current.batch_keys) > 100:
                raise PurgeBlocked("object batch exceeds bound")
            removed = 0
            for item in current.batch_keys:
                ref = ObjectRef(item["key"])
                if not (ref.key.startswith(f"orgs/{org_id}/")
                        or ref.key.startswith("exports/")):
                    raise PurgeBlocked("object is outside purge ownership")
                if ref.key.startswith("exports/"):
                    request_id = UUID(ref.key.removeprefix("exports/").removesuffix(".zip"))
                    owner = await db.scalar(select(ExportRequest.org_id).where(
                        ExportRequest.id == request_id,
                    ))
                    if owner != org_id:
                        raise PurgeBlocked("export object ownership is unverified")
                existed = await object_store.exists(ref)
                await object_store.delete(ref, reason=DeletionReason.SWEEP)
                if await object_store.exists(ref):
                    raise PurgeBlocked("object deletion is unverified")
                if ref.key.startswith("exports/"):
                    await db.execute(sql_text(
                        "update export_request set bundle_object_key=null, "
                        "status=case when status='ready' then 'failed' else status end "
                        "where org_id=:org and id=:request and bundle_object_key=:key"
                    ), {"org": org_id, "request": request_id, "key": ref.key})
                removed += int(existed)
            return removed
        if current.step_key == "capabilities" and current.target_table == "sessions":
            if len(current.batch_keys) > 100:
                raise PurgeBlocked("session batch exceeds bound")
            revoked = 0
            for item in current.batch_keys:
                revoked += await sessions.revoke_all(UUID(item["account_id"]))
            return revoked
        if current.step_key == "derived" and current.target_table == "queue_jobs":
            ids = [int(item["job_id"]) for item in current.batch_keys]
            await db.execute(sql_text(
                "update procrastinate_jobs set status='cancelled' "
                "where id=any(cast(:ids as bigint[])) and status='todo'"
            ), {"ids": ids})
            doing = await db.scalar(sql_text(
                "select count(*) from procrastinate_jobs "
                "where id=any(cast(:ids as bigint[])) and status='doing'"
            ), {"ids": ids})
            if doing:
                raise PurgeBlocked("ordinary worker still owns an inventoried job")
            return len(ids)
        if current.target_table is None:
            raise PurgeBlocked("database batch has no target table")
        if current.target_table == "org":
            raise PurgeBlocked("organization row requires durable completion authorization")
        return int((await db.execute(
            sql_text("select app_execute_org_purge_batch(:step)"),
            {"step": current.id},
        )).scalar_one())


async def prepare_step_completion(
    session: AsyncSession, *, step_id: UUID, deleted_count: int,
    expected_sequence: int,
) -> OrgLifecycleOperation:
    """Fence the independently durable completion of one batch."""
    step = (await session.execute(select(OrgPurgeStep).where(
        OrgPurgeStep.id == step_id,
    ).with_for_update())).scalar_one()
    run = (await session.execute(select(OrgPurgeRun).where(
        OrgPurgeRun.id == step.purge_run_id,
    ).with_for_update())).scalar_one()
    _, _, operation_id = step_ids(run.id, step.step_key, step.batch_key)
    existing = await session.get(OrgLifecycleOperation, operation_id)
    if existing is not None:
        return existing
    org = (await session.execute(select(Org).where(
        Org.id == run.org_id,
    ).with_for_update())).scalar_one_or_none()
    if (org is None or org.lifecycle_status != "purging"
            or org.offboarding_id != run.offboarding_id
            or org.lifecycle_sequence != expected_sequence
            or run.execution_epoch != step.execution_epoch
            or step.status not in {"authorized", "failed"}):
        raise PurgeBlocked("batch completion is stale")
    operation = OrgLifecycleOperation(
        id=operation_id, org_id=run.org_id,
        service_term_id=run.service_term_id,
        offboarding_id=run.offboarding_id,
        action="complete_destructive_step",
        expected_sequence=expected_sequence,
        actor_ops_account_id=None, reason="verified purge batch",
        retention_policy_reference=run.retention_policy_reference,
        command_payload={
            "run_id": str(run.id), "step_id": str(step.id),
            "step_key": step.step_key, "batch_key": step.batch_key,
            "manifest_digest": step.batch_manifest_digest,
            "execution_epoch": step.execution_epoch,
            "deleted_count": deleted_count,
        },
    )
    session.add(operation)
    await session.flush()
    return operation


async def apply_step_completion(
    session: AsyncSession, *, operation_id: UUID, event: HistoryEvent,
) -> OrgPurgeStep:
    """Project a verified batch outcome without losing its independent receipt."""
    operation = (await session.execute(select(OrgLifecycleOperation).where(
        OrgLifecycleOperation.id == operation_id,
    ).with_for_update())).scalar_one()
    step = (await session.execute(select(OrgPurgeStep).where(
        OrgPurgeStep.id == UUID(operation.command_payload["step_id"]),
    ).with_for_update())).scalar_one()
    if operation.status == "applied":
        return step
    if (operation.status != "pending" or event.action != "complete_destructive_step"
            or event.operation_id != operation.id or event.org_id != operation.org_id
            or event.data != operation.command_payload
            or event.sequence != operation.expected_sequence + 1):
        raise PurgeBlocked("batch completion event mismatch")
    run = (await session.execute(select(OrgPurgeRun).where(
        OrgPurgeRun.id == step.purge_run_id,
    ).with_for_update())).scalar_one()
    org = (await session.execute(select(Org).where(
        Org.id == run.org_id,
    ).with_for_update())).scalar_one_or_none()
    if (org is None or org.lifecycle_sequence != operation.expected_sequence
            or run.execution_epoch != step.execution_epoch):
        raise PurgeBlocked("batch completion projection is stale")
    step.status = "completed"
    step.completed_at = datetime.now(UTC)
    step.deleted_count = int(event.data["deleted_count"])
    step.failure_class = None
    org.lifecycle_sequence = event.sequence
    operation.status = "applied"
    operation.resulting_sequence = event.sequence
    operation.resolved_at = datetime.now(UTC)
    await session.flush()
    return step


async def complete_step(
    *, org_id: UUID, step_id: UUID, deleted_count: int,
    history: LifecycleHistory,
) -> OrgPurgeStep:
    """Durably record and project a completed batch, safe to replay."""
    head = await history.verified_head(org_id)
    async with scoped_transaction(system_scope(org_id=org_id)) as db:
        operation = await prepare_step_completion(
            db, step_id=step_id, deleted_count=deleted_count,
            expected_sequence=head.sequence,
        )
    event = await history.append(
        org_id=org_id, operation_id=operation.id,
        expected_sequence=operation.expected_sequence,
        action=operation.action, accepted_at=operation.created_at.isoformat(),
        actor_id=None, reason=operation.reason,
        data=operation.command_payload,
    )
    async with scoped_transaction(system_scope(org_id=org_id)) as db:
        return await apply_step_completion(
            db, operation_id=operation.id, event=event,
        )


def final_operation_id(run_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"bluelab:org-purge:{run_id}:complete")


async def prepare_final_completion(
    session: AsyncSession, *, run_id: UUID, org_step_id: UUID,
    expected_sequence: int, summary: dict[str, Any],
) -> OrgLifecycleOperation:
    """Fence final deletion after every other category passes verification."""
    operation_id = final_operation_id(run_id)
    existing = await session.get(OrgLifecycleOperation, operation_id)
    if existing is not None:
        return existing
    run = (await session.execute(select(OrgPurgeRun).where(
        OrgPurgeRun.id == run_id,
    ).with_for_update())).scalar_one()
    org = (await session.execute(select(Org).where(
        Org.id == run.org_id,
    ).with_for_update())).scalar_one_or_none()
    if (org is None or org.lifecycle_status != "purging"
            or org.offboarding_id != run.offboarding_id
            or org.lifecycle_sequence != expected_sequence
            or run.status != "running" or await _claim_blocker(session, run.org_id)):
        raise PurgeBlocked("final purge decision is blocked")
    steps = (await session.execute(select(OrgPurgeStep).where(
        OrgPurgeStep.purge_run_id == run_id,
    ))).scalars().all()
    target = next((step for step in steps if step.id == org_step_id), None)
    if (target is None or target.status != "authorized"
            or target.target_table != "org"
            or target.execution_epoch != run.execution_epoch
            or any(step.status != "completed" for step in steps
                   if step.id != org_step_id
                   and step.execution_epoch == run.execution_epoch)):
        raise PurgeBlocked("purge batch evidence is incomplete")
    operation = OrgLifecycleOperation(
        id=operation_id, org_id=run.org_id,
        service_term_id=run.service_term_id,
        offboarding_id=run.offboarding_id,
        action="complete", expected_sequence=expected_sequence,
        actor_ops_account_id=None, reason="verified organization purge",
        retention_policy_reference=run.retention_policy_reference,
        command_payload={
            "run_id": str(run_id), "org_step_id": str(org_step_id),
            "inventory_manifest_digest": run.inventory_manifest_digest,
            "deletion_rule_version": run.deletion_rule_version,
            "summary": summary,
        },
    )
    session.add(operation)
    await session.flush()
    return operation


async def apply_final_completion(
    session: AsyncSession, *, operation_id: UUID, event: HistoryEvent,
) -> OrgPurgeRun:
    """Delete the last org row only under the accepted completion decision."""
    operation = (await session.execute(select(OrgLifecycleOperation).where(
        OrgLifecycleOperation.id == operation_id,
    ).with_for_update())).scalar_one()
    run = (await session.execute(select(OrgPurgeRun).where(
        OrgPurgeRun.id == UUID(operation.command_payload["run_id"]),
    ).with_for_update())).scalar_one()
    if operation.status == "applied" and run.status == "completed":
        return run
    if (operation.status != "pending" or event.action != "complete"
            or event.operation_id != operation.id or event.org_id != operation.org_id
            or event.data != operation.command_payload
            or event.sequence != operation.expected_sequence + 1):
        raise PurgeBlocked("final completion event mismatch")
    org = (await session.execute(select(Org).where(
        Org.id == run.org_id,
    ).with_for_update())).scalar_one_or_none()
    if (org is None or org.lifecycle_sequence != operation.expected_sequence
            or org.offboarding_id != run.offboarding_id
            or await _claim_blocker(session, run.org_id)):
        raise PurgeBlocked("final completion projection is stale")
    deleted = int((await session.execute(
        sql_text("select app_execute_org_purge_batch(:step)"),
        {"step": UUID(event.data["org_step_id"])},
    )).scalar_one())
    if deleted != 1:
        raise PurgeBlocked("organization row deletion was not verified")
    step = (await session.execute(select(OrgPurgeStep).where(
        OrgPurgeStep.id == UUID(event.data["org_step_id"]),
    ).with_for_update())).scalar_one()
    step.status = "completed"
    step.completed_at = datetime.now(UTC)
    step.deleted_count = 1
    run.status = "completed"
    run.completed_at = datetime.now(UTC)
    run.verification_summary = event.data["summary"]
    operation.status = "applied"
    operation.resulting_sequence = event.sequence
    operation.resolved_at = datetime.now(UTC)
    await session.flush()
    return run


async def complete_run(
    *, org_id: UUID, run_id: UUID, org_step_id: UUID,
    summary: dict[str, Any], history: LifecycleHistory,
) -> OrgPurgeRun:
    """Commit independent completion before projecting the final deletion."""
    head = await history.verified_head(org_id)
    async with scoped_transaction(system_scope(org_id=org_id)) as db:
        operation = await prepare_final_completion(
            db, run_id=run_id, org_step_id=org_step_id,
            expected_sequence=head.sequence, summary=summary,
        )
    event = await history.append(
        org_id=org_id, operation_id=operation.id,
        expected_sequence=operation.expected_sequence,
        action="complete", accepted_at=operation.created_at.isoformat(),
        actor_id=None, reason=operation.reason,
        data=operation.command_payload,
    )
    async with scoped_transaction(system_scope(org_id=org_id)) as db:
        return await apply_final_completion(
            db, operation_id=operation.id, event=event,
        )


async def _inventory_run(
    *, org_id: UUID, run_id: UUID, history: LifecycleHistory,
    object_store: RestoreObjectStore,
) -> dict[str, Any]:
    """Pin the complete key inventory before any locating parent is deleted."""
    key: str | None
    digest: str | None
    async with scoped_transaction(system_scope(org_id=org_id)) as db:
        run = (await db.execute(select(OrgPurgeRun).where(
            OrgPurgeRun.id == run_id,
        ).with_for_update())).scalar_one_or_none()
        if run is None or run.org_id != org_id:
            raise PurgeBlocked("purge run is missing")
        if run.status == "pending":
            run.status = "running"
            run.execution_epoch = 1
            run.purge_started_at = datetime.now(UTC)
            run.last_progress_at = datetime.now(UTC)
        elif run.status in {"retry_pending", "paused_restriction"}:
            if run.retry_at and run.retry_at > datetime.now(UTC):
                raise PurgeBlocked("purge retry is not due")
            if await _claim_blocker(db, org_id):
                raise PurgeBlocked("purge restriction remains active")
            run.execution_epoch += 1
            run.status = "running"
            run.retry_at = None
        elif run.status != "running":
            raise PurgeBlocked("purge run requires operator resolution")
        if run.inventory_manifest_key and run.inventory_manifest_digest:
            key, digest = run.inventory_manifest_key, run.inventory_manifest_digest
        else:
            key = digest = None
    if key and digest:
        return await history.read_purge_record(key, digest)
    async with scoped_transaction(system_scope(org_id=org_id)) as db:
        rows = await inventory_rows(db, org_id)
        objects = await inventory_objects(db, org_id, object_store)
        jobs = await inventory_jobs(db, org_id)
    manifest: dict[str, Any] = {
        "rule_version": "org-purge:v1", "org_id": str(org_id),
        "run_id": str(run_id), "rows": rows, "objects": objects,
        "jobs": jobs,
    }
    key = f"org-purge/{org_id}/{run_id}/inventory.json"
    digest = await history.put_purge_record(key, manifest)
    async with scoped_transaction(system_scope(org_id=org_id)) as db:
        run = (await db.execute(select(OrgPurgeRun).where(
            OrgPurgeRun.id == run_id,
        ).with_for_update())).scalar_one()
        if run.inventory_manifest_digest and run.inventory_manifest_digest != digest:
            raise PurgeBlocked("purge inventory projection differs")
        if run.status != "running":
            raise PurgeBlocked("purge run requires recovery before execution")
        run.deletion_rule_version = "org-purge:v1"
        run.inventory_manifest_key = key
        run.inventory_manifest_digest = digest
        await db.flush()
    return manifest


async def _verify_cleanup(
    *, org_id: UUID, initial: dict[str, Any],
    object_store: RestoreObjectStore,
) -> dict[str, Any]:
    """Category-complete zero-remain check before deleting the org row."""
    async with scoped_transaction(system_scope(org_id=org_id)) as db:
        remaining = await inventory_rows(db, org_id)
        jobs = await inventory_jobs(db, org_id)
    nonzero = {
        table: len(rows) for table, rows in remaining.items()
        if table != "org" and rows
    }
    if nonzero:
        raise PurgeBlocked("database purge categories remain")
    if jobs:
        raise PurgeBlocked("runnable organization jobs remain")
    if len(remaining["org"]) != 1:
        raise PurgeBlocked("organization row is not uniquely present")
    for key in initial["objects"]:
        if await object_store.exists(ObjectRef(key)):
            raise PurgeBlocked("inventoried object remains")
    return {
        "rule_version": "org-purge:v1",
        "category_rows": {
            step: sum(len(initial["rows"][table]) for table in STEP_TABLES.get(step, ()))
            for step in STEP_ORDER
        },
        "objects": len(initial["objects"]),
        "verified_at": datetime.now(UTC).isoformat(),
    }


async def record_failure(
    *, org_id: UUID, run_id: UUID, step_id: UUID | None, error: Exception,
) -> None:
    """Keep a failed run discoverable with sanitized bounded retry state."""
    instant = datetime.now(UTC)
    async with scoped_transaction(system_scope(org_id=org_id)) as db:
        run = (await db.execute(select(OrgPurgeRun).where(
            OrgPurgeRun.id == run_id,
        ).with_for_update())).scalar_one()
        if run.status == "completed":
            return
        blocker = await _claim_blocker(db, org_id)
        if blocker:
            run.status = "paused_restriction"
            run.retry_at = None
        else:
            run.failure_count += 1
            if (run.failure_count >= 5 or
                    (run.last_progress_at is not None
                     and instant - run.last_progress_at >= timedelta(hours=24))):
                run.status = "needs_attention"
                run.retry_at = None
            else:
                run.status = "retry_pending"
                run.retry_at = instant + timedelta(
                    seconds=min(3600, 300 * (2 ** (run.failure_count - 1))),
                )
        run.last_failure_class = type(error).__name__[:80]
        if step_id is not None:
            step = await db.get(OrgPurgeStep, step_id)
            if step is not None and step.status != "completed":
                step.status = "failed"
                step.failure_class = run.last_failure_class


async def record_progress(*, org_id: UUID, run_id: UUID) -> None:
    """Reset the consecutive-failure window after a verified batch."""
    async with scoped_transaction(system_scope(org_id=org_id)) as db:
        run = (await db.execute(select(OrgPurgeRun).where(
            OrgPurgeRun.id == run_id,
        ).with_for_update())).scalar_one()
        run.failure_count = 0
        run.last_progress_at = datetime.now(UTC)
        run.last_failure_class = None


async def execute_run(
    *, org_id: UUID, run_id: UUID, history: LifecycleHistory,
    object_store: RestoreObjectStore, sessions: SessionStore,
) -> OrgPurgeRun:
    """Resume ordered bounded batches from a verified independent inventory."""
    inventory = await _inventory_run(
        org_id=org_id, run_id=run_id, history=history,
        object_store=object_store,
    )
    if (inventory.get("org_id") != str(org_id)
            or inventory.get("run_id") != str(run_id)
            or inventory.get("rule_version") != "org-purge:v1"):
        raise PurgeBlocked("purge inventory identity or rule version mismatch")
    account_keys = [
        {"account_id": entry["id"]} for entry in inventory["rows"]["account"]
    ]
    ordered: list[dict[str, Any]] = []
    for offset in range(0, len(account_keys), 100):
        ordered.append({
            "step_key": "capabilities", "target_table": "sessions",
            "batch_key": f"sessions:{offset // 100:08d}",
            "keys": account_keys[offset:offset + 100],
        })
    row_batches = batch_keys(inventory["rows"])
    ordered.extend(batch for batch in row_batches if batch["step_key"] == "capabilities")
    for offset in range(0, len(inventory["objects"]), 100):
        ordered.append({
            "step_key": "objects", "target_table": None,
            "batch_key": f"objects:{offset // 100:08d}",
            "keys": [{"key": key} for key in inventory["objects"][offset:offset + 100]],
        })
    ordered.extend(batch for batch in row_batches if batch["step_key"] != "capabilities")
    for offset in range(0, len(inventory["jobs"]), 1000):
        ordered.append({
            "step_key": "derived", "target_table": "queue_jobs",
            "batch_key": f"queue_jobs:{offset // 1000:08d}",
            "keys": [{"job_id": str(job_id)} for job_id in inventory["jobs"][offset:offset + 1000]],
        })
    org_batch: dict[str, Any] | None = None
    for batch in ordered:
        if batch["target_table"] == "org":
            org_batch = batch
            continue
        step: OrgPurgeStep | None = None
        try:
            step = await authorize_step(
                org_id=org_id, run_id=run_id,
                step_key=batch["step_key"], batch_key=batch["batch_key"],
                target_table=batch["target_table"], keys=batch["keys"],
                history=history,
            )
            if step.status == "completed":
                continue
            deleted = await execute_step(
                org_id=org_id, step=step, object_store=object_store,
                history=history, sessions=sessions,
            )
            await complete_step(
                org_id=org_id, step_id=step.id,
                deleted_count=deleted, history=history,
            )
            await record_progress(org_id=org_id, run_id=run_id)
        except Exception as exc:
            await record_failure(
                org_id=org_id, run_id=run_id,
                step_id=step.id if step else None, error=exc,
            )
            raise
    if org_batch is None:
        raise PurgeBlocked("organization row absent from purge inventory")
    summary = await _verify_cleanup(
        org_id=org_id, initial=inventory, object_store=object_store,
    )
    step = await authorize_step(
        org_id=org_id, run_id=run_id,
        step_key="org_finalize", batch_key=org_batch["batch_key"],
        target_table="org", keys=org_batch["keys"], history=history,
    )
    return await complete_run(
        org_id=org_id, run_id=run_id, org_step_id=step.id,
        summary=summary, history=history,
    )
