"""`/ops/v1` — the BlueLab-internal surface, separately authenticated (ADR-0010).
The path split is what makes the separation routable and auditable.
"""

from __future__ import annotations

from typing import Annotated, Literal, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from sqlalchemy import select, text

from bluelab.adapters.lifecycle_history import (
    HistoryConflict,
    HistoryUnverified,
    create_lifecycle_history,
)
from bluelab.adapters.object_store import AuthorizedObjectRead, ObjectRef
from bluelab.api.deps import (
    ClientAddress,
    DeliverySecretSealerDep,
    ObjectStoreDep,
    OpsPrincipal,
    OpsSessionStoreDep,
    OpsTotpUnsealerDep,
    SessionStoreDep,
    TotpReplayStoreDep,
    ValkeyDep,
    enforce_ops_sign_in_rate,
    object_store_from_request,
    ops_session_cookie,
)
from bluelab.calls.lease import CallLeaseStore
from bluelab.lifecycle import service as lifecycle
from bluelab.modules.identity import service as identity_service
from bluelab.modules.operations import faults, service
from bluelab.modules.operations.models import OrgLifecycleOperation
from bluelab.modules.operations.schemas import (
    AuditPage,
    ErasureRequestPage,
    ErasureRequestView,
    ExportRequestPage,
    ExportRequestView,
    FaultPage,
    FaultView,
    OpsAccountCreate,
    OpsAccountView,
    OpsOrgCreate,
    OpsOrgView,
    OpsReasonBody,
    OpsSignInRequest,
    OpsSignInView,
    OrgLifecycleView,
    OrgRetentionPolicyView,
    OrgServiceTermCommand,
    PositionTransferRequest,
    ResolveFaultRequest,
    ServiceTermPreviewRequest,
    ServiceTermPreviewView,
    SubjectRequestCreate,
    TeamChangeRequest,
    canonical_domain,
)
from bluelab.platform.clock import now, retention_deadline, service_term_bounds
from bluelab.platform.config import Settings, get_settings
from bluelab.platform.db.privileged import ops_scope
from bluelab.platform.db.scope import ScopeContext
from bluelab.platform.db.session import scoped_transaction
from bluelab.platform.errors import catalog
from bluelab.platform.errors.denial import ProblemError, not_found
from bluelab.platform.resilience import DependencyUnavailable
from bluelab.platform.security.cookies import (
    OPS_SESSION_COOKIE,
    CookieSpec,
    clear_session_cookie,
    session_spec,
    set_session_cookie,
)
from bluelab.platform.security.sessions import SessionRevokedDuringCreation

PREFIX = "/ops/v1"
router = APIRouter(
    prefix=PREFIX,
    tags=["Ops"],
)

SettingsDep = Annotated[Settings, Depends(get_settings)]


def _spec(settings: Settings) -> CookieSpec:
    return session_spec(
        OPS_SESSION_COOKIE,
        max_age=settings.session_absolute_seconds,
        secure=settings.cookie_secure,
    )


@router.post(
    "/session",
    dependencies=[Depends(enforce_ops_sign_in_rate)],
    operation_id="opsSignIn",
    response_model=OpsSignInView,
    status_code=status.HTTP_200_OK,
    summary="Ops sign-in",
)
async def sign_in(
    payload: OpsSignInRequest,
    response: Response,
    store: OpsSessionStoreDep,
    unsealer: OpsTotpUnsealerDep,
    replay: TotpReplayStoreDep,
    source: ClientAddress,
    settings: SettingsDep,
) -> OpsSignInView:
    """Require password plus non-replayed TOTP and open a separate session."""
    opened_at = now()
    async with scoped_transaction(ScopeContext.anonymous()) as db:
        authenticated = await service.authenticate_ops(
            db,
            email=str(payload.email),
            password=payload.password,
            totp_code=payload.totp_code,
            at=opened_at,
            unsealer=unsealer,
            replay=replay,
            source=source,
        )
    revocation_epoch = await store.revocation_epoch(authenticated.ops_account_id)
    try:
        async with scoped_transaction(
            ops_scope(ops_account_id=authenticated.ops_account_id)
        ) as db:
            current = await service.load_ops_account(db, authenticated.ops_account_id)
            if current.password_hash != authenticated.password_hash:
                raise ProblemError(catalog.INVALID_CREDENTIALS)
            raw = await store.create(
                ops_account_id=authenticated.ops_account_id,
                opened_at=opened_at,
                revocation_epoch=revocation_epoch,
            )
    except SessionRevokedDuringCreation:
        raise ProblemError(catalog.OPS_SESSION_INVALID) from None
    set_session_cookie(response, _spec(settings), raw)
    return OpsSignInView(
        ops_account_id=authenticated.ops_account_id,
        display_name=authenticated.display_name,
        session_expires_at=store.new_session_expires_at(opened_at),
    )


@router.delete(
    "/session",
    operation_id="opsSignOut",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Ops sign-out",
)
async def sign_out(
    response: Response,
    _record: OpsPrincipal,
    store: OpsSessionStoreDep,
    settings: SettingsDep,
    raw: Annotated[str, Depends(ops_session_cookie)],
) -> None:
    await store.revoke(raw)
    clear_session_cookie(response, _spec(settings))


@router.post(
    "/service-term-preview",
    operation_id="opsPreviewServiceTerm",
    response_model=ServiceTermPreviewView,
    summary="Calculate service and retention boundaries",
)
async def preview_service_term(
    payload: ServiceTermPreviewRequest, _record: OpsPrincipal
) -> ServiceTermPreviewView:
    starts_at, ends_at = service_term_bounds(
        payload.service_start_on, payload.service_last_access_on, payload.timezone
    )
    return ServiceTermPreviewView(
        service_starts_at=starts_at,
        service_ends_at=ends_at,
        projected_purge_eligible_at=retention_deadline(
            ends_at,
            payload.period_value,
            payload.period_unit,
            payload.timezone if payload.period_unit != "elapsed_days" else None,
        ),
    )


@router.get(
    "/orgs/{org_id}", operation_id="opsGetOrgLifecycle",
    response_model=OrgLifecycleView, summary="Inspect organization lifecycle",
)
async def get_org_lifecycle(
    org_id: UUID, record: OpsPrincipal, settings: SettingsDep
) -> OrgLifecycleView:
    history = create_lifecycle_history(settings)
    try:
        head = await history.verified_head(org_id)
    except HistoryUnverified as exc:
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED) from exc
    async with scoped_transaction(
        ops_scope(ops_account_id=UUID(record.ops_account_id))
    ) as db:
        return await lifecycle.lifecycle_view(db, org_id, head)


@router.get(
    "/orgs/{org_id}/retention-policy",
    operation_id="opsGetOrgRetentionPolicy",
    response_model=OrgRetentionPolicyView,
    summary="Inspect an organization's verified retention policy",
)
async def get_org_retention_policy(
    org_id: UUID, record: OpsPrincipal, settings: SettingsDep
) -> OrgRetentionPolicyView:
    history = create_lifecycle_history(settings)
    try:
        head = await history.verified_head(org_id)
    except HistoryUnverified as exc:
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED) from exc
    async with scoped_transaction(
        ops_scope(ops_account_id=UUID(record.ops_account_id))
    ) as db:
        return await lifecycle.retention_policy_view(db, org_id, head)


@router.post(
    "/orgs/{org_id}/service-terms",
    operation_id="opsConfirmOrgServiceTerm",
    response_model=OrgLifecycleView,
    summary="Confirm an organization service term",
)
async def confirm_org_service_term(
    org_id: UUID,
    payload: OrgServiceTermCommand,
    record: OpsPrincipal,
    settings: SettingsDep,
    operation_id: Annotated[UUID, Header(alias="Lifecycle-Operation-Id")],
) -> OrgLifecycleView:
    actor_id = UUID(record.ops_account_id)
    history = create_lifecycle_history(settings)
    try:
        head = await history.verified_head(org_id)
    except HistoryUnverified as exc:
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED) from exc
    async with scoped_transaction(ops_scope(ops_account_id=actor_id)) as db:
        operation = await lifecycle.prepare_term(
            db, org_id=org_id, operation_id=operation_id, actor_id=actor_id,
            command=payload, head=head,
        )
    try:
        event = await history.append(
            org_id=org_id, operation_id=operation_id,
            expected_sequence=operation.expected_sequence,
            action=operation.action, accepted_at=operation.created_at.isoformat(),
            actor_id=actor_id, reason=operation.reason,
            data=operation.command_payload,
        )
    except HistoryConflict as exc:
        async with scoped_transaction(ops_scope(ops_account_id=actor_id)) as db:
            await lifecycle.reject_term(db, operation_id)
        raise ProblemError(catalog.LIFECYCLE_SEQUENCE_STALE) from exc
    except HistoryUnverified as exc:
        raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED) from exc
    async with scoped_transaction(ops_scope(ops_account_id=actor_id)) as db:
        await lifecycle.apply_term(db, operation_id=operation_id, event=event)
    return await get_org_lifecycle(org_id, record, settings)


@router.post(
    "/orgs",
    operation_id="opsCreateOrg",
    response_model=OpsOrgView,
    status_code=status.HTTP_201_CREATED,
    summary="Provision a customer org",
)
async def create_org(
    payload: OpsOrgCreate,
    record: OpsPrincipal,
    settings: SettingsDep,
    operation_id: Annotated[UUID | None, Header(alias="Lifecycle-Operation-Id")] = None,
) -> OpsOrgView:
    actor_id = UUID(record.ops_account_id)
    command = None
    if payload.service_start_on is not None:
        if operation_id is None or payload.service_last_access_on is None or payload.contract_reference is None:
            raise ProblemError(catalog.SERVICE_TERM_INVALID)
        command = OrgServiceTermCommand(
            expected_sequence=0,
            service_start_on=payload.service_start_on,
            service_last_access_on=payload.service_last_access_on,
            contract_reference=payload.contract_reference,
            retention_policy=payload.retention_policy,
            reason=payload.reason,
        )
    if command is not None and operation_id is not None:
        history = create_lifecycle_history(settings)
        async with scoped_transaction(ops_scope(ops_account_id=actor_id)) as db:
            existing = (await db.execute(
                select(OrgLifecycleOperation).where(OrgLifecycleOperation.id == operation_id)
            )).scalar_one_or_none()
            if existing is None:
                org = await identity_service.provision_org(
                    db, name=payload.name,
                    registered_domain=payload.registered_domain,
                    timezone=payload.timezone,
                )
                try:
                    head = await history.verified_head(org.id)
                except HistoryUnverified as exc:
                    raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED) from exc
                await lifecycle.prepare_term(
                    db, org_id=org.id, operation_id=operation_id,
                    actor_id=actor_id, command=command, head=head,
                )
                await service.append_audit(
                    db, verb="provision_org", target_org_id=org.id,
                    target_ref={"org_id": str(org.id)},
                    reason=service.require_reason(payload.reason),
                )
            else:
                projection = await identity_service.lifecycle_projection(db, existing.org_id)
                if projection is None:
                    raise ProblemError(catalog.LIFECYCLE_HISTORY_UNVERIFIED)
                org = projection[0]
                if (
                    org.name != payload.name or org.registered_domain != payload.registered_domain
                    or org.timezone != payload.timezone
                ):
                    raise ProblemError(catalog.OPERATION_ID_REUSE)
        confirmed = await confirm_org_service_term(
            org.id, command, record, settings, operation_id
        )
        return OpsOrgView(
            org_id=org.id, name=org.name,
            registered_domain=org.registered_domain, timezone=org.timezone,
            service_starts_at=confirmed.service_starts_at,
            service_ends_at=confirmed.service_ends_at,
            projected_purge_eligible_at=confirmed.projected_purge_eligible_at,
        )
    async with scoped_transaction(
        ops_scope(ops_account_id=actor_id)
    ) as db:
        reason = service.require_reason(payload.reason)
        org = await identity_service.provision_org(
            db,
            name=payload.name,
            registered_domain=payload.registered_domain,
            timezone=payload.timezone,
        )
        await service.append_audit(
            db,
            verb="provision_org",
            target_org_id=org.id,
            target_ref={"org_id": str(org.id)},
            reason=reason,
        )
        return OpsOrgView(
            org_id=org.id,
            name=org.name,
            registered_domain=org.registered_domain,
            timezone=org.timezone,
        )


def _raise_provision_outcome(
    outcome: identity_service.ProvisionAccountOutcome,
) -> NoReturn:
    """Map a committed, audited provisioning refusal onto the HTTP contract."""
    if outcome is identity_service.ProvisionAccountOutcome.DUPLICATE_EMAIL:
        raise ProblemError(catalog.DUPLICATE_EMAIL)
    if outcome in {
        identity_service.ProvisionAccountOutcome.ORG_NOT_FOUND,
        identity_service.ProvisionAccountOutcome.MANAGER_NOT_FOUND,
    }:
        raise not_found()
    raise ProblemError(catalog.VALIDATION_ERROR)


@router.post(
    "/accounts",
    operation_id="opsCreateAccount",
    response_model=OpsAccountView,
    status_code=status.HTTP_201_CREATED,
    summary="Provision an account",
)
async def create_account(
    payload: OpsAccountCreate,
    record: OpsPrincipal,
    sealer: DeliverySecretSealerDep,
) -> OpsAccountView:
    normalized_email = str(payload.email).strip().lower()
    reason = service.require_reason(payload.reason)
    async with scoped_transaction(
        ops_scope(ops_account_id=UUID(record.ops_account_id))
    ) as db:
        result = await identity_service.provision_account(
            db,
            org_id=payload.org_id,
            email=normalized_email,
            display_name=payload.display_name,
            role=payload.role,
            manager_account_id=payload.manager_account_id,
            sealer=sealer,
        )
        target_ref: dict[str, object] = {
            "email_domain": canonical_domain(normalized_email.rsplit("@", 1)[1]),
            "outcome": result.outcome.value,
            "role": payload.role,
        }
        if result.outcome is identity_service.ProvisionAccountOutcome.CREATED:
            target_ref["account_id"] = str(result.account_id)
        elif result.outcome is identity_service.ProvisionAccountOutcome.ORG_NOT_FOUND:
            target_ref["requested_org_id"] = str(payload.org_id)
        if payload.manager_account_id is not None:
            target_ref["manager_account_id"] = str(payload.manager_account_id)
        await service.append_audit(
            db,
            verb="provision_account",
            target_org_id=(
                None
                if result.outcome
                is identity_service.ProvisionAccountOutcome.ORG_NOT_FOUND
                else payload.org_id
            ),
            target_ref=target_ref,
            reason=reason,
        )
    if result.outcome is not identity_service.ProvisionAccountOutcome.CREATED:
        _raise_provision_outcome(result.outcome)
    return OpsAccountView(
        account_id=result.account_id,
        email=normalized_email,
        role=payload.role,
    )


@router.post(
    "/accounts/{account_id}/deactivate",
    operation_id="opsDeactivateAccount",
    status_code=status.HTTP_200_OK,
    summary="Deactivate an account",
)
async def deactivate_account(
    account_id: UUID,
    payload: OpsReasonBody,
    record: OpsPrincipal,
    customer_sessions: SessionStoreDep,
) -> dict[str, object]:
    reason = service.require_reason(payload.reason)
    async with scoped_transaction(
        ops_scope(ops_account_id=UUID(record.ops_account_id))
    ) as db:
        result = await service.deactivate_account(db, account_id=account_id)
        await service.append_audit(
            db,
            verb="deactivate_account",
            target_org_id=result.org_id,
            target_ref={"account_id": str(account_id)},
            reason=reason,
        )
    await customer_sessions.revoke_all(account_id)
    return {"account_id": account_id, "status": "deactivated"}


@router.post(
    "/accounts/{account_id}/team-change",
    operation_id="opsChangeTeam",
    status_code=status.HTTP_200_OK,
    summary="Reassign a rep to another manager",
)
async def change_team(
    account_id: UUID,
    payload: TeamChangeRequest,
    record: OpsPrincipal,
    customer_sessions: SessionStoreDep,
) -> dict[str, UUID]:
    reason = service.require_reason(payload.reason)
    async with scoped_transaction(
        ops_scope(ops_account_id=UUID(record.ops_account_id))
    ) as db:
        org_id, team_id = await service.change_team(
            db,
            account_id=account_id,
            new_manager_account_id=payload.new_manager_account_id,
        )
        await service.append_audit(
            db,
            verb="change_team_mapping",
            target_org_id=org_id,
            target_ref={
                "account_id": str(account_id),
                "new_manager_account_id": str(payload.new_manager_account_id),
            },
            reason=reason,
        )
    await customer_sessions.revoke_all(account_id)
    return {"account_id": account_id, "team_id": team_id}


@router.post(
    "/positions/{position_id}/transfer",
    operation_id="opsTransferPosition",
    status_code=status.HTTP_200_OK,
    summary="Transfer a position to another manager",
)
async def transfer_position(
    position_id: UUID,
    payload: PositionTransferRequest,
    record: OpsPrincipal,
) -> dict[str, UUID]:
    reason = service.require_reason(payload.reason)
    async with scoped_transaction(
        ops_scope(ops_account_id=UUID(record.ops_account_id))
    ) as db:
        org_id, team_id = await service.transfer_position(
            db,
            position_id=position_id,
            new_manager_account_id=payload.new_manager_account_id,
        )
        await service.append_audit(
            db,
            verb="transfer_position",
            target_org_id=org_id,
            target_ref={
                "position_id": str(position_id),
                "new_manager_account_id": str(payload.new_manager_account_id),
            },
            reason=reason,
        )
    return {"position_id": position_id, "team_id": team_id}


def _valid_cursor(cursor: str | None) -> str | None:
    if cursor == "":
        raise ProblemError(catalog.VALIDATION_ERROR)
    return cursor


@router.get(
    "/erasure-requests",
    operation_id="opsListErasureRequests",
    response_model=ErasureRequestPage,
)
async def list_erasure_requests(
    record: OpsPrincipal,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> ErasureRequestPage:
    async with scoped_transaction(
        ops_scope(ops_account_id=UUID(record.ops_account_id))
    ) as db:
        result = await service.list_subject_requests(
            db, request_kind="erasure", cursor=_valid_cursor(cursor), limit=limit
        )
        assert isinstance(result, ErasureRequestPage)
        return result


@router.post(
    "/erasure-requests",
    operation_id="opsCreateErasureRequest",
    response_model=ErasureRequestView,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_erasure_request(
    payload: SubjectRequestCreate,
    record: OpsPrincipal,
    customer_sessions: SessionStoreDep,
    valkey: ValkeyDep,
) -> ErasureRequestView:
    reason = service.require_reason(payload.reason)
    async with scoped_transaction(
        ops_scope(ops_account_id=UUID(record.ops_account_id))
    ) as db:
        result = await service.create_subject_request(
            db,
            request_kind="erasure",
            org_id=payload.org_id,
            subject_kind=payload.subject_kind,
            subject_id=payload.subject_id,
            executed_by=UUID(record.ops_account_id),
        )
        await service.append_audit(
            db,
            verb="execute_erasure",
            target_org_id=payload.org_id,
            target_ref={
                "request_id": str(result.id),
                "subject_kind": payload.subject_kind,
                "subject_id": str(payload.subject_id),
            },
            reason=reason,
        )
        assert isinstance(result, ErasureRequestView)
    if payload.subject_kind == "account":
        await customer_sessions.revoke_all(payload.subject_id)
        participant_kind: Literal["rep", "candidate"] = "rep"
    else:
        participant_kind = "candidate"
    leases = CallLeaseStore(valkey)
    active = await leases.current(participant_kind, payload.subject_id)
    if active is not None:
        await leases.release(active)
    return result


@router.get(
    "/erasure-requests/{request_id}",
    operation_id="opsGetErasureRequest",
    response_model=ErasureRequestView,
)
async def get_erasure_request(
    request_id: UUID, record: OpsPrincipal
) -> ErasureRequestView:
    async with scoped_transaction(
        ops_scope(ops_account_id=UUID(record.ops_account_id))
    ) as db:
        result = await service.get_subject_request(
            db, request_kind="erasure", request_id=request_id, bundle_url=None
        )
        assert isinstance(result, ErasureRequestView)
        return result


@router.get(
    "/export-requests",
    operation_id="opsListExportRequests",
    response_model=ExportRequestPage,
)
async def list_export_requests(
    record: OpsPrincipal,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> ExportRequestPage:
    async with scoped_transaction(
        ops_scope(ops_account_id=UUID(record.ops_account_id))
    ) as db:
        result = await service.list_subject_requests(
            db, request_kind="export", cursor=_valid_cursor(cursor), limit=limit
        )
        assert isinstance(result, ExportRequestPage)
        return result


@router.post(
    "/export-requests",
    operation_id="opsCreateExportRequest",
    response_model=ExportRequestView,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_export_request(
    payload: SubjectRequestCreate, record: OpsPrincipal
) -> ExportRequestView:
    reason = service.require_reason(payload.reason)
    async with scoped_transaction(
        ops_scope(ops_account_id=UUID(record.ops_account_id))
    ) as db:
        result = await service.create_subject_request(
            db,
            request_kind="export",
            org_id=payload.org_id,
            subject_kind=payload.subject_kind,
            subject_id=payload.subject_id,
            executed_by=UUID(record.ops_account_id),
        )
        await service.append_audit(
            db,
            verb="execute_export",
            target_org_id=payload.org_id,
            target_ref={
                "request_id": str(result.id),
                "subject_kind": payload.subject_kind,
                "subject_id": str(payload.subject_id),
            },
            reason=reason,
        )
        assert isinstance(result, ExportRequestView)
        return result


@router.get(
    "/export-requests/{request_id}",
    operation_id="opsGetExportRequest",
    response_model=ExportRequestView,
)
async def get_export_request(
    request_id: UUID,
    record: OpsPrincipal,
    object_store: ObjectStoreDep,
) -> ExportRequestView:
    bundle_url: str | None = None
    async with scoped_transaction(
        ops_scope(ops_account_id=UUID(record.ops_account_id))
    ) as db:
        result = await service.get_subject_request(
            db, request_kind="export", request_id=request_id, bundle_url=None
        )
        assert isinstance(result, ExportRequestView)
        if (
            result.status in {"ready", "delivered"}
            and result.expires_at is not None
            and result.expires_at > now()
        ):
            ref = ObjectRef.export(request_id=request_id)
            signed = await object_store.presign_get(
                AuthorizedObjectRead(
                    object_ref=ref,
                    principal_id=UUID(record.ops_account_id),
                    authorized_until=result.expires_at,
                )
            )
            bundle_url = signed.url
            await db.execute(
                text(
                    "update export_request set status='delivered' where id=:id and status='ready'"
                ),
                {"id": request_id},
            )
        return result.model_copy(
            update={"status": "delivered" if bundle_url else result.status, "bundle_url": bundle_url}
        )


@router.get(
    "/audit",
    operation_id="opsListAudit",
    response_model=AuditPage,
    summary="Ops audit trail",
)
async def list_audit(
    record: OpsPrincipal,
    org_id: UUID | None = None,
    cursor: Annotated[
        str | None,
        Query(description="Opaque pagination cursor from a previous response."),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> AuditPage:
    async with scoped_transaction(
        ops_scope(ops_account_id=UUID(record.ops_account_id))
    ) as db:
        return await service.list_audit(
            db,
            org_id=org_id,
            cursor=cursor,
            limit=limit,
        )


@router.get(
    "/faults",
    operation_id="opsListFaults",
    response_model=FaultPage,
    status_code=status.HTTP_200_OK,
    summary="Fault queue",
)
async def list_faults(
    record: OpsPrincipal,
    status_filter: Annotated[
        faults.FaultFilter,
        Query(alias="status", description="Open, resolved, or all faults."),
    ] = "open",
    cursor: Annotated[
        str | None,
        Query(description="Opaque pagination cursor from a previous response."),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> FaultPage:
    async with scoped_transaction(
        ops_scope(ops_account_id=UUID(record.ops_account_id))
    ) as db:
        return await faults.list_faults(
            db, status=status_filter, cursor=cursor, limit=limit
        )


@router.post(
    "/faults/{fault_id}/resolve",
    operation_id="opsResolveFault",
    response_model=FaultView,
    status_code=status.HTTP_200_OK,
    summary="Resolve a fault (re-drive the job)",
)
async def resolve_fault(
    fault_id: UUID,
    payload: ResolveFaultRequest,
    record: OpsPrincipal,
    request: Request,
    settings: SettingsDep,
) -> FaultView:
    playback_available: bool | None = None
    async with scoped_transaction(
        ops_scope(ops_account_id=UUID(record.ops_account_id))
    ) as db:
        identity = await faults.load_fault(db, fault_id=fault_id)
    if identity.kind == "playback_asset" and identity.status == "open":
        try:
            object_store = object_store_from_request(request, settings)
            playback_available = await object_store.exists(
                ObjectRef.recording(
                    org_id=identity.org_id, attempt_id=identity.attempt_id
                )
            )
        except DependencyUnavailable:
            raise ProblemError(catalog.SERVICE_UNAVAILABLE) from None

    async with scoped_transaction(
        ops_scope(ops_account_id=UUID(record.ops_account_id))
    ) as db:
        return await faults.resolve_fault(
            db,
            fault_id=fault_id,
            reason=payload.reason,
            playback_available=playback_available,
        )
