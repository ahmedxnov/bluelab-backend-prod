"""Replay verified lifecycle decisions behind the isolated restore barrier."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.adapters.erasure_ledger import ErasureLedger, ErasureMarker
from bluelab.adapters.lifecycle_history import HistoryEvent, LifecycleHistory
from bluelab.adapters.object_store import DeletionReason, ObjectRef, RestoreObjectStore
from bluelab.lifecycle import restrictions, service, subject_requests, subject_states
from bluelab.lifecycle.purge import (
    _verify_cleanup,
    apply_claim,
    apply_final_completion,
    apply_step_authorization,
    apply_step_completion,
    execute_step,
)
from bluelab.modules.identity.models import Org, OrgRetentionPolicy
from bluelab.modules.operations.models import (
    ErasureRequest,
    ExportRequest,
    OpsAudit,
    OrgDeletionRestriction,
    OrgLifecycleOperation,
    OrgPurgeRun,
    OrgPurgeStep,
)
from bluelab.modules.operations.subject_rights import execute_erasure
from bluelab.platform.db.privileged import system_scope
from bluelab.platform.db.session import scoped_transaction
from bluelab.platform.queue.catalog import Lane
from bluelab.platform.queue.enqueue import enqueue
from bluelab.platform.security.sessions import SessionStore


class RestoreUnverified(RuntimeError):
    """The restored state must remain isolated for operator resolution."""


async def ensure_replay_pending(db: AsyncSession, marker: ErasureMarker) -> bool:
    """Reopen an executed request whenever restored rows fail zero-remain."""
    existing = (await db.execute(text(
        "select org_id,subject_kind,subject_id from erasure_request "
        "where id=:request for update"
    ), {"request": marker.request_id})).first()
    if existing is not None and (
        existing.org_id != marker.org_id
        or existing.subject_kind != marker.subject_kind
        or existing.subject_id != marker.subject_id
    ):
        raise RestoreUnverified("restored erasure request conflicts with its marker")
    await db.execute(text(
        "select app_restore_erasure_marker(:request,:org,:kind,:subject,"
        ":requested,:actor)"
    ), {
        "request": marker.request_id, "org": marker.org_id,
        "kind": marker.subject_kind, "subject": marker.subject_id,
        "requested": marker.requested_at, "actor": marker.executed_by,
    })
    remaining = dict((await db.execute(
        text("select app_verify_erasure(:request)"),
        {"request": marker.request_id},
    )).scalar_one())
    needs_replay = any(int(count) for count in remaining.values())
    if needs_replay:
        await db.execute(text(
            "update erasure_request set status='pending' "
            "where id=:request and status='executed'"
        ), {"request": marker.request_id})
    return needs_replay


def _uuid(value: object) -> UUID | None:
    return UUID(str(value)) if value else None


async def _ensure_operation(event: HistoryEvent) -> None:
    """Rebuild the durable pending fence lost with a database rollback."""
    data = event.data
    async with scoped_transaction(system_scope(org_id=event.org_id)) as db:
        org = await db.get(Org, event.org_id)
        if org is None or org.lifecycle_sequence != event.sequence - 1:
            raise RestoreUnverified("lifecycle projection cannot accept verified event")
        existing = await db.get(OrgLifecycleOperation, event.operation_id)
        if existing is not None:
            if (existing.status != "pending" or existing.action != event.action
                    or existing.expected_sequence != event.sequence - 1
                    or existing.command_payload != data or existing.reason != event.reason
                    or existing.actor_ops_account_id != event.actor_id):
                raise RestoreUnverified("restored lifecycle operation disagrees with history")
            return
        pending = (await db.execute(select(OrgLifecycleOperation).where(
            OrgLifecycleOperation.org_id == event.org_id,
            OrgLifecycleOperation.status == "pending",
        ).with_for_update())).scalar_one_or_none()
        if pending is not None:
            # The authority has no accepted event for this local-only fence.
            pending.status = "rejected"
            pending.resolved_at = datetime.fromisoformat(event.accepted_at)
            await db.flush()
        policy = data.get("retention_policy")
        db.add(OrgLifecycleOperation(
            id=event.operation_id, org_id=event.org_id,
            service_term_id=_uuid(data.get("service_term_id")),
            offboarding_id=_uuid(data.get("offboarding_id")
                                   or data.get("cancelled_offboarding_id")
                                   or org.offboarding_id),
            action=event.action, expected_sequence=event.sequence - 1,
            actor_ops_account_id=event.actor_id, reason=event.reason,
            retention_policy_reference=(data.get("retention_policy_reference")
                                        or policy.get("policy_reference") if isinstance(policy, dict)
                                        else data.get("retention_policy_reference")),
            restriction_id=_uuid(data.get("restriction_id")),
            command_payload=data,
            created_at=datetime.fromisoformat(event.accepted_at),
        ))
        await db.flush()


async def _replay_marker(
    marker: ErasureMarker, *, ledger: ErasureLedger,
    object_store: RestoreObjectStore,
) -> None:
    async with scoped_transaction(system_scope(org_id=marker.org_id)) as db:
        request = await db.get(ErasureRequest, marker.request_id)
        if request is None:
            return
        needs_work = await ensure_replay_pending(db, marker)
    if needs_work:
        async with scoped_transaction(system_scope(org_id=marker.org_id)) as db:
            await execute_erasure(
                db, request_id=marker.request_id,
                ledger=ledger, object_store=object_store,
            )


async def _apply_simple(event: HistoryEvent) -> None:
    action = event.action
    async with scoped_transaction(system_scope(org_id=event.org_id)) as db:
        if action in {"confirm_term", "renew_term"}:
            await service.apply_term(db, operation_id=event.operation_id,
                                     event=event, replay_by_system=True)
        elif action == "expire_term":
            await service.apply_expiry(db, operation_id=event.operation_id, event=event)
        elif action == "start":
            await service.apply_start_offboarding(
                db, operation_id=event.operation_id, event=event, replay_by_system=True)
        elif action == "cancel":
            await service.apply_cancel_offboarding(
                db, operation_id=event.operation_id, event=event, replay_by_system=True)
        elif action == "policy_revision":
            await service.apply_policy_revision(
                db, operation_id=event.operation_id, event=event, replay_by_system=True)
        elif action == "extend":
            await service.apply_deadline_extension(
                db, operation_id=event.operation_id, event=event, replay_by_system=True)
        elif action == "create_restriction" and "request_kind" in event.data:
            await subject_requests.apply(
                db, operation_id=event.operation_id, event=event, replay_by_system=True)
        elif action == "create_restriction" or (
            action == "release_restriction" and "request_kind" not in event.data
        ):
            await restrictions.apply(
                db, operation_id=event.operation_id, event=event, replay_by_system=True)
        elif action in {"set_subject_request_state", "release_restriction"}:
            await subject_states.apply(
                db, operation_id=event.operation_id, event=event, replay_by_system=True)
        elif action == "claim":
            await apply_claim(db, operation_id=event.operation_id, event=event)
        else:
            raise RestoreUnverified("unsupported lifecycle event in restore history")


async def _replay_purge_event(
    event: HistoryEvent, *, history: LifecycleHistory,
    object_store: RestoreObjectStore, sessions: SessionStore,
) -> None:
    data = event.data
    run_id = _uuid(data.get("run_id"))
    if run_id is None:
        raise RestoreUnverified("purge event has no run identity")
    inventory_key = f"org-purge/{event.org_id}/{run_id}/inventory.json"
    digest, inventory = await history.verified_purge_record(inventory_key)
    if (inventory.get("org_id") != str(event.org_id)
            or inventory.get("run_id") != str(run_id)
            or inventory.get("rule_version") != "org-purge:v1"):
        raise RestoreUnverified("purge inventory identity mismatch")
    if (event.action == "complete"
            and data.get("inventory_manifest_digest") != digest):
        raise RestoreUnverified("completion does not pin the verified purge inventory")
    async with scoped_transaction(system_scope(org_id=event.org_id)) as db:
        run = await db.get(OrgPurgeRun, run_id, with_for_update=True)
        if run is None or run.org_id != event.org_id or run.status == "completed":
            raise RestoreUnverified("purge run projection is missing or terminal")
        if run.inventory_manifest_digest not in {None, digest}:
            raise RestoreUnverified("purge inventory projection differs from authority")
        run.inventory_manifest_key = inventory_key
        run.inventory_manifest_digest = digest
        run.deletion_rule_version = "org-purge:v1"
        run.status = "running"
        if event.action == "authorize_destructive_step":
            epoch = int(data["execution_epoch"])
            if epoch < run.execution_epoch:
                raise RestoreUnverified("purge event precedes restored execution epoch")
            run.execution_epoch = epoch
    if event.action == "authorize_destructive_step":
        manifest = await history.read_purge_record(
            str(data["manifest_key"]), str(data["manifest_digest"]),
        )
        if (manifest.get("org_id") != str(event.org_id)
                or manifest.get("run_id") != str(run_id)):
            raise RestoreUnverified("purge batch ownership differs from authority")
        async with scoped_transaction(system_scope(org_id=event.org_id)) as db:
            await apply_step_authorization(db, operation_id=event.operation_id,
                                           event=event, manifest=manifest)
    elif event.action == "complete_destructive_step":
        async with scoped_transaction(system_scope(org_id=event.org_id)) as db:
            step = await db.get(OrgPurgeStep, UUID(data["step_id"]))
            if step is None or step.batch_manifest_digest != data["manifest_digest"]:
                raise RestoreUnverified("purge step completion has no verified authorization")
        await execute_step(org_id=event.org_id, step=step, object_store=object_store,
                           history=history, sessions=sessions)
        async with scoped_transaction(system_scope(org_id=event.org_id)) as db:
            await apply_step_completion(db, operation_id=event.operation_id, event=event)
    elif event.action == "complete":
        await reconcile_completed_purge_objects(
            event.org_id, run_id, history=history,
            object_store=object_store, inventory=inventory,
        )
        await _verify_cleanup(org_id=event.org_id, initial=inventory,
                              object_store=object_store)
        async with scoped_transaction(system_scope(org_id=event.org_id)) as db:
            await apply_final_completion(db, operation_id=event.operation_id, event=event)
    else:
        raise RestoreUnverified("unsupported purge event")


async def reconcile_completed_purge_objects(
    org_id: UUID, run_id: UUID, *, history: LifecycleHistory,
    object_store: RestoreObjectStore, inventory: dict[str, Any] | None = None,
) -> None:
    """Repeat verified completed object batches after an object-only rollback."""
    if inventory is None:
        _, inventory = await history.verified_purge_record(
            f"org-purge/{org_id}/{run_id}/inventory.json",
        )
    if (inventory.get("org_id") != str(org_id)
            or inventory.get("run_id") != str(run_id)
            or inventory.get("rule_version") != "org-purge:v1"
            or not isinstance(inventory.get("objects"), list)):
        raise RestoreUnverified("completed purge object inventory is unverified")
    authorized_keys = set(inventory["objects"])
    async with scoped_transaction(system_scope(org_id=org_id)) as db:
        steps = (await db.execute(select(OrgPurgeStep).where(
            OrgPurgeStep.purge_run_id == run_id,
            OrgPurgeStep.step_key == "objects",
            OrgPurgeStep.status == "completed",
        ))).scalars().all()
    for step in steps:
        manifest = await history.read_purge_record(
            step.batch_manifest_key, step.batch_manifest_digest,
        )
        keys = manifest.get("keys")
        if (manifest.get("org_id") != str(org_id)
                or manifest.get("run_id") != str(run_id)
                or keys != step.batch_keys or not isinstance(keys, list)):
            raise RestoreUnverified("completed object batch has unverified ownership")
        for item in keys:
            key = item["key"]
            if key not in authorized_keys:
                raise RestoreUnverified("completed object key is absent from inventory")
            ref = ObjectRef(key)
            await object_store.delete(ref, reason=DeletionReason.SWEEP)
            if await object_store.exists(ref):
                raise RestoreUnverified("restored purge object remains readable")


async def replay_organization(
    org_id: UUID, events: Sequence[HistoryEvent], *, history: LifecycleHistory,
    markers: Sequence[ErasureMarker], ledger: ErasureLedger,
    object_store: RestoreObjectStore, sessions: SessionStore,
) -> None:
    """Advance a restored organization only along its verified event sequence."""
    async with scoped_transaction(system_scope(org_id=org_id)) as db:
        absent = await db.get(Org, org_id) is None
        if absent:
            completed_run_id = await db.scalar(select(OrgPurgeRun.id).where(
                OrgPurgeRun.org_id == org_id, OrgPurgeRun.status == "completed",
            ))
            if completed_run_id is None or not events or events[-1].action != "complete":
                raise RestoreUnverified("absent organization lacks completed purge evidence")
            for event in events:
                operation = await db.get(OrgLifecycleOperation, event.operation_id)
                if (operation is None or operation.status != "applied"
                        or operation.resulting_sequence != event.sequence
                        or operation.command_payload != event.data
                        or operation.actor_ops_account_id != event.actor_id
                        or operation.reason != event.reason):
                    raise RestoreUnverified("completed purge history lacks a projection")
    if absent:
        return
    for event in events:
        async with scoped_transaction(system_scope(org_id=org_id)) as db:
            org = await db.get(Org, org_id)
            operation = await db.get(OrgLifecycleOperation, event.operation_id)
            if org is None:
                if (event.action != "complete" or operation is None
                        or operation.status != "applied"):
                    raise RestoreUnverified("organization is absent before verified completion")
                continue
            if org.lifecycle_sequence >= event.sequence:
                if (operation is None or operation.status != "applied"
                        or operation.resulting_sequence != event.sequence
                        or operation.action != event.action
                        or operation.command_payload != event.data
                        or operation.actor_ops_account_id != event.actor_id
                        or operation.reason != event.reason):
                    raise RestoreUnverified("restored operation lacks matching authority")
                continue
            if org.lifecycle_sequence != event.sequence - 1:
                raise RestoreUnverified("restored lifecycle sequence has a gap")
        await _ensure_operation(event)
        if event.action in {"claim", "authorize_destructive_step", "complete"}:
            for marker in markers:
                if marker.org_id == org_id:
                    await _replay_marker(marker, ledger=ledger, object_store=object_store)
        if event.action in {
            "authorize_destructive_step", "complete_destructive_step", "complete",
        }:
            await _replay_purge_event(event, history=history,
                                      object_store=object_store, sessions=sessions)
        else:
            await _apply_simple(event)
    for marker in markers:
        if marker.org_id == org_id:
            await _replay_marker(marker, ledger=ledger, object_store=object_store)
    async with scoped_transaction(system_scope(org_id=org_id)) as db:
        org = await db.get(Org, org_id)
        if org is not None:
            latest_policy = next((event for event in reversed(events)
                                  if event.action == "policy_revision" or (
                                      event.action in {"confirm_term", "renew_term"}
                                      and event.data.get("policy_is_revision")
                                  )), None)
            if latest_policy is not None:
                expected_policy = (latest_policy.data["retention_policy"]
                                   if latest_policy.action != "policy_revision"
                                   else latest_policy.data)
                policy = await db.get(OrgRetentionPolicy, org_id)
                if (policy is None
                        or policy.policy_reference != expected_policy["policy_reference"]
                        or policy.effective_sequence != latest_policy.sequence):
                    raise RestoreUnverified("retention policy projection is stale")
            orphan = (await db.execute(text(
                "select id from org_deletion_restriction r where r.org_id=:org "
                "and r.status='active' and r.related_request_id is not null "
                "and not exists (select 1 from erasure_request e where "
                "e.id=r.related_request_id and e.org_id=r.org_id) "
                "and not exists (select 1 from export_request x where "
                "x.id=r.related_request_id and x.org_id=r.org_id) limit 1"
            ), {"org": org_id})).scalar_one_or_none()
            if orphan is not None and not any(
                event.action == "create_restriction"
                and event.data.get("restriction_id") == str(orphan)
                for event in events
            ):
                raise RestoreUnverified("orphaned request restriction lacks authority")
            for event in events:
                if event.action != "create_restriction" or "request_kind" not in event.data:
                    continue
                data = event.data
                kind = data["request_kind"]
                if kind not in {"export", "erasure"}:
                    raise RestoreUnverified("subject request history has unknown kind")
                request_id = UUID(data["request_id"])
                restriction_id = UUID(data["restriction_id"])
                model = ExportRequest if kind == "export" else ErasureRequest
                request = cast(ExportRequest | ErasureRequest | None,
                               await db.get(model, request_id))
                restriction = await db.get(OrgDeletionRestriction, restriction_id)
                if request is None or restriction is None:
                    later_state = any(
                        later.sequence > event.sequence
                        and later.data.get("request_id") == str(request_id)
                        for later in events
                    )
                    if later_state:
                        raise RestoreUnverified(
                            "resolved subject request projection needs operator reconstruction"
                        )
                    accepted_at = datetime.fromisoformat(event.accepted_at)
                    if restriction is None:
                        restriction = OrgDeletionRestriction(
                            id=restriction_id, org_id=org_id,
                            offboarding_id=_uuid(data.get("offboarding_id")),
                            scope=data["restriction_scope"], reason=event.reason,
                            authority_ref=data["authority_ref"],
                            release_condition=data["release_condition"],
                            related_request_id=request_id, created_at=accepted_at,
                        )
                        db.add(restriction)
                    if request is None:
                        request_fields = {
                            "id": request_id, "org_id": org_id,
                            "subject_kind": data["subject_kind"],
                            "subject_id": UUID(data["subject_id"]),
                            "status": "pending",
                            "request_policy_reference": data["request_policy_reference"],
                            "restriction_id": restriction_id,
                            "requested_at": accepted_at,
                        }
                        if kind == "erasure":
                            request = ErasureRequest(
                                **request_fields, executed_by=event.actor_id,
                            )
                        else:
                            request = ExportRequest(**request_fields)
                        db.add(request)
                    audit_id = UUID(data["audit_id"])
                    if await db.get(OpsAudit, audit_id) is None:
                        if event.actor_id is None:
                            raise RestoreUnverified("subject request has no operator authority")
                        db.add(OpsAudit(
                            id=audit_id, ops_account_id=event.actor_id,
                            verb="execute_export" if kind == "export" else "execute_erasure",
                            target_org_id=org_id,
                            target_ref={
                                "request_id": str(request_id),
                                "restriction_id": str(restriction_id),
                                "subject_kind": data["subject_kind"],
                                "subject_id": data["subject_id"],
                            },
                            reason=event.reason, occurred_at=accepted_at,
                        ))
                    jobs = (await db.execute(text(
                        "select count(*) from procrastinate_jobs where "
                        "args->'args'->>'request_id'=:request"
                    ), {"request": str(request_id)})).scalar_one()
                    if not jobs:
                        await db.flush()
                        await enqueue(db, Lane(data["worker_lane"]),
                                      {"request_id": str(request_id)}, org_id=org_id)
                if (request is None or restriction is None
                        or request.org_id != org_id
                        or request.restriction_id != restriction_id
                        or restriction.org_id != org_id
                        or restriction.related_request_id != request_id):
                    raise RestoreUnverified(
                        "accepted subject request or restriction projection is missing"
                    )
        completed = (await db.execute(select(OrgPurgeRun).where(
            OrgPurgeRun.org_id == org_id, OrgPurgeRun.status == "completed",
        ))).scalar_one_or_none()
        if org is None:
            if not events or events[-1].action != "complete" or completed is None:
                raise RestoreUnverified("absent organization lacks completed purge evidence")
        elif org.lifecycle_sequence != (events[-1].sequence if events else 0):
            raise RestoreUnverified("restored organization is behind verified history")
