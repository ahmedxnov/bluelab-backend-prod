"""`/ops/v1` — the BlueLab-internal surface, separately authenticated (ADR-0010).
The path split is what makes the separation routable and auditable.
"""

from __future__ import annotations

from typing import Annotated, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status

from bluelab.adapters.object_store import ObjectRef
from bluelab.api.deps import (
    ClientAddress,
    DeliverySecretSealerDep,
    OpsPrincipal,
    OpsSessionStoreDep,
    OpsTotpUnsealerDep,
    TotpReplayStoreDep,
    enforce_ops_sign_in_rate,
    object_store_from_request,
    ops_session_cookie,
)
from bluelab.modules.identity import service as identity_service
from bluelab.modules.operations import faults, service
from bluelab.modules.operations.schemas import (
    AuditPage,
    FaultPage,
    FaultView,
    OpsAccountCreate,
    OpsAccountView,
    OpsOrgCreate,
    OpsOrgView,
    OpsSignInRequest,
    OpsSignInView,
    ResolveFaultRequest,
    canonical_domain,
)
from bluelab.platform.clock import now
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
    "/orgs",
    operation_id="opsCreateOrg",
    response_model=OpsOrgView,
    status_code=status.HTTP_201_CREATED,
    summary="Provision a customer org",
)
async def create_org(payload: OpsOrgCreate, record: OpsPrincipal) -> OpsOrgView:
    async with scoped_transaction(
        ops_scope(ops_account_id=UUID(record.ops_account_id))
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
