"""Domain services — this module's behaviour, and its published interface to the
other modules. Cross-module callers enter here; they never touch `models.py`.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, NoReturn
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.adapters.secrets import (
    OpsTotpContext,
    SecretEnvelopeError,
    SecretUnsealer,
)
from bluelab.modules.operations.models import OpsAccount
from bluelab.modules.operations.schemas import (
    AuditEntry,
    AuditPage,
    CursorPage,
    ErasureRequestPage,
    ErasureRequestView,
    ExportRequestPage,
    ExportRequestView,
    SubjectKind,
)
from bluelab.platform.config import Settings
from bluelab.platform.errors import catalog
from bluelab.platform.errors.denial import ProblemError, not_found
from bluelab.platform.ids import new_id
from bluelab.platform.queue.catalog import Lane
from bluelab.platform.queue.enqueue import enqueue
from bluelab.platform.security import passwords
from bluelab.platform.security.throttle import Limit, Throttle
from bluelab.platform.security.tokens import hash_token
from bluelab.platform.security.totp import TotpReplayStore, matching_step
from bluelab.platform.telemetry import metrics
from bluelab.platform.telemetry.logging import get_logger

_log = get_logger("bluelab.operations")

_OPS_SIGN_IN = text(
    "select ops_account_id, display_name, password_hash,"
    " totp_secret_ciphertext, status from app_ops_account_for_sign_in(:email)"
)
_APPEND_AUDIT = text(
    "select app_append_ops_audit("
    ":id, :verb, :target_org_id, cast(:target_ref as jsonb), :reason)"
)


@dataclass(frozen=True, slots=True)
class AuthenticatedOps:
    ops_account_id: UUID
    display_name: str
    password_hash: str


async def guard_ops_sign_in(
    throttle: Throttle, settings: Settings, *, source: str
) -> None:
    """Enforce the ops sign-in surface's fixed five-requests-per-minute limit."""
    retry = await throttle.hit(
        "ops_source",
        source,
        Limit(settings.ops_throttle_attempts, settings.ops_throttle_window_seconds),
    )
    if retry is not None:
        metrics.record_auth_signal(metrics.AuthSignal.THROTTLE_TRIPPED)
        _log.warning("ops_auth_signal", signal="throttle_tripped", source_id=hash_token(source))
        raise ProblemError(catalog.RATE_LIMITED, headers={"Retry-After": str(retry)})


def _invalid_ops_credentials(*, email: str, source: str) -> NoReturn:
    metrics.record_auth_signal(metrics.AuthSignal.SIGN_IN_FAILED)
    _log.warning(
        "ops_auth_signal",
        signal="sign_in_failed",
        identifier_id=hash_token(email.strip().lower()),
        source_id=hash_token(source),
    )
    raise ProblemError(catalog.INVALID_CREDENTIALS)


async def authenticate_ops(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    totp_code: str,
    at: datetime,
    unsealer: SecretUnsealer,
    replay: TotpReplayStore,
    source: str,
) -> AuthenticatedOps:
    """Verify password then TOTP and atomically consume the accepted time step."""
    row = (
        await session.execute(_OPS_SIGN_IN, {"email": email.strip().lower()})
    ).one_or_none()
    if row is None:
        passwords.dummy_verify()
        _invalid_ops_credentials(email=email, source=source)
    if not passwords.verify_password(password, row.password_hash):
        _invalid_ops_credentials(email=email, source=source)
    if row.status != "active":
        _invalid_ops_credentials(email=email, source=source)

    ops_account_id = UUID(str(row.ops_account_id))
    try:
        seed = await unsealer.unseal(
            bytes(row.totp_secret_ciphertext),
            context=OpsTotpContext(ops_account_id=ops_account_id),
        )
        step = matching_step(seed, totp_code, at=at)
    except (SecretEnvelopeError, ValueError):
        step = None
    if step is None or not await replay.claim(
        ops_account_id=ops_account_id, step=step
    ):
        _invalid_ops_credentials(email=email, source=source)

    return AuthenticatedOps(
        ops_account_id=ops_account_id,
        display_name=str(row.display_name),
        password_hash=str(row.password_hash),
    )


async def load_ops_account(
    session: AsyncSession,
    ops_account_id: UUID,
) -> OpsAccount:
    """Re-read active operator state for every protected request."""
    statement = select(OpsAccount).where(OpsAccount.id == ops_account_id)
    account = (await session.execute(statement)).scalar_one_or_none()
    if account is None or account.status != "active":
        raise ProblemError(catalog.OPS_SESSION_INVALID)
    return account


def require_reason(reason: str) -> str:
    """Return a trimmed audit reason or raise the dedicated contract problem."""
    value = reason.strip()
    if not value:
        raise ProblemError(catalog.REASON_REQUIRED)
    return value


async def append_audit(
    session: AsyncSession,
    *,
    verb: str,
    target_org_id: UUID | None,
    target_ref: dict[str, object],
    reason: str,
) -> None:
    """Append one actor-bound audit entry through the narrow database helper."""
    await session.execute(
        _APPEND_AUDIT,
        {
            "id": new_id(),
            "verb": verb,
            "target_org_id": target_org_id,
            "target_ref": json.dumps(target_ref, separators=(",", ":"), sort_keys=True),
            "reason": require_reason(reason),
        },
    )


@dataclass(frozen=True, slots=True)
class _AuditCursor:
    org_id: UUID | None
    occurred_at: datetime
    audit_id: UUID

    def encode(self) -> str:
        payload = json.dumps(
            {
                "audit_id": str(self.audit_id),
                "occurred_at": self.occurred_at.isoformat(),
                "org_id": str(self.org_id) if self.org_id is not None else None,
                "v": 1,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return base64.urlsafe_b64encode(payload).decode()

    @classmethod
    def decode(cls, cursor: str, *, org_id: UUID | None) -> _AuditCursor:
        try:
            raw = base64.b64decode(cursor.encode(), altchars=b"-_", validate=True)
            data = json.loads(raw)
            if set(data) != {"audit_id", "occurred_at", "org_id", "v"} or data["v"] != 1:
                raise ValueError
            decoded = cls(
                org_id=UUID(data["org_id"]) if data["org_id"] is not None else None,
                occurred_at=datetime.fromisoformat(data["occurred_at"]),
                audit_id=UUID(data["audit_id"]),
            )
            if decoded.occurred_at.tzinfo is None:
                raise ValueError
        except (
            ValueError,
            TypeError,
            KeyError,
            UnicodeDecodeError,
            binascii.Error,
            json.JSONDecodeError,
        ) as exc:
            raise ProblemError(
                catalog.VALIDATION_ERROR,
                detail="cursor is not one this server issued",
            ) from exc
        if decoded.org_id != org_id:
            raise ProblemError(
                catalog.VALIDATION_ERROR,
                detail="cursor does not belong to this audit filter",
            )
        return decoded


_LIST_AUDIT = text(
    """
    select id, ops_account_id, verb, target_org_id, target_ref, reason, occurred_at
      from ops_audit
     where (cast(:org_id as uuid) is null or target_org_id = cast(:org_id as uuid))
       and (
            cast(:cursor_at as timestamptz) is null
            or (occurred_at, id) < (
                cast(:cursor_at as timestamptz), cast(:cursor_id as uuid)
            )
       )
     order by occurred_at desc, id desc
     limit :limit
    """
)


async def list_audit(
    session: AsyncSession,
    *,
    org_id: UUID | None,
    cursor: str | None,
    limit: int,
) -> AuditPage:
    """Return newest-first append-only audit entries with a filter-bound cursor."""
    position = _AuditCursor.decode(cursor, org_id=org_id) if cursor is not None else None
    rows = (
        await session.execute(
            _LIST_AUDIT,
            {
                "org_id": org_id,
                "cursor_at": position.occurred_at if position else None,
                "cursor_id": position.audit_id if position else None,
                "limit": limit + 1,
            },
        )
    ).all()
    has_more = len(rows) > limit
    page = rows[:limit]
    data = [
        AuditEntry(
            id=row.id,
            ops_account_id=row.ops_account_id,
            verb=row.verb,
            target_org_id=row.target_org_id,
            target_ref=dict(row.target_ref),
            reason=row.reason,
            occurred_at=row.occurred_at,
        )
        for row in page
    ]
    next_cursor = (
        _AuditCursor(
            org_id=org_id,
            occurred_at=page[-1].occurred_at,
            audit_id=page[-1].id,
        ).encode()
        if has_more and page
        else None
    )
    return AuditPage(
        data=data,
        pagination=CursorPage(next_cursor=next_cursor, has_more=has_more),
    )


@dataclass(frozen=True, slots=True)
class DeactivationResult:
    account_id: UUID
    org_id: UUID
    status: Literal["deactivated"]


async def deactivate_account(
    session: AsyncSession, *, account_id: UUID
) -> DeactivationResult:
    """Run the ops-only deactivation boundary with dependent checks."""
    row = (
        await session.execute(
            text("select * from app_deactivate_account(:account_id)"),
            {"account_id": account_id},
        )
    ).mappings().one_or_none()
    if row is None:
        raise ProblemError(catalog.SUBJECT_UNKNOWN)
    if int(row["dependent_reps"]) or int(row["open_positions"]):
        raise ProblemError(
            catalog.MANAGER_OWNS_DEPENDENTS,
            meta={
                "reps": int(row["dependent_reps"]),
                "open_positions": int(row["open_positions"]),
            },
        )
    return DeactivationResult(
        account_id=account_id, org_id=UUID(str(row["org_id"])), status="deactivated"
    )


async def change_team(
    session: AsyncSession, *, account_id: UUID, new_manager_account_id: UUID
) -> tuple[UUID, UUID]:
    """Move one rep and every scope-cascading historical child atomically."""
    row = (
        await session.execute(
            text("select * from app_change_team(:account_id,:manager_id)"),
            {"account_id": account_id, "manager_id": new_manager_account_id},
        )
    ).one_or_none()
    if row is None:
        raise ProblemError(catalog.VALIDATION_ERROR)
    return UUID(str(row.org_id)), UUID(str(row.team_id))


async def transfer_position(
    session: AsyncSession, *, position_id: UUID, new_manager_account_id: UUID
) -> tuple[UUID, UUID]:
    """Transfer a position subtree while leaving personal HR contacts in place."""
    row = (
        await session.execute(
            text("select * from app_transfer_position(:position_id,:manager_id)"),
            {"position_id": position_id, "manager_id": new_manager_account_id},
        )
    ).one_or_none()
    if row is None:
        raise ProblemError(catalog.VALIDATION_ERROR)
    return UUID(str(row.org_id)), UUID(str(row.team_id))


@dataclass(frozen=True, slots=True)
class _RequestCursor:
    requested_at: datetime
    request_id: UUID

    def encode(self) -> str:
        raw = json.dumps(
            {"id": str(self.request_id), "requested_at": self.requested_at.isoformat(), "v": 1},
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return base64.urlsafe_b64encode(raw).decode()

    @classmethod
    def decode(cls, value: str) -> _RequestCursor:
        try:
            raw = base64.b64decode(value.encode(), altchars=b"-_", validate=True)
            data = json.loads(raw)
            if set(data) != {"id", "requested_at", "v"} or data["v"] != 1:
                raise ValueError
            requested_at = datetime.fromisoformat(data["requested_at"])
            if requested_at.tzinfo is None:
                raise ValueError
            return cls(requested_at=requested_at, request_id=UUID(data["id"]))
        except (ValueError, TypeError, KeyError, binascii.Error, json.JSONDecodeError) as exc:
            raise ProblemError(catalog.VALIDATION_ERROR, detail="cursor is not one this server issued") from exc


async def create_subject_request(
    session: AsyncSession,
    *,
    request_kind: Literal["erasure", "export"],
    org_id: UUID,
    subject_kind: SubjectKind,
    subject_id: UUID,
    executed_by: UUID,
) -> ErasureRequestView | ExportRequestView:
    """Validate one subject and transactionally enqueue its rights request."""
    request_id = new_id()
    await session.execute(
        text("select pg_advisory_xact_lock(hashtextextended(:subject, 0))"),
        {"subject": str(subject_id)},
    )
    valid = (
        await session.execute(
            text("select app_subject_request_valid(:org,:kind,:subject,:erasure)"),
            {
                "org": org_id,
                "kind": subject_kind,
                "subject": subject_id,
                "erasure": request_kind == "erasure",
            },
        )
    ).scalar_one()
    if not valid:
        raise ProblemError(catalog.SUBJECT_UNKNOWN)
    table = "erasure_request" if request_kind == "erasure" else "export_request"
    columns = ",executed_by" if request_kind == "erasure" else ""
    values = ",:actor" if request_kind == "erasure" else ""
    await session.execute(
        text(
            f"insert into {table}(id,org_id,subject_kind,subject_id{columns}) "
            f"values(:id,:org,:kind,:subject{values})"
        ),
        {
            "id": request_id,
            "org": org_id,
            "kind": subject_kind,
            "subject": subject_id,
            "actor": executed_by,
        },
    )
    lane = Lane.EXECUTE_ERASURE if request_kind == "erasure" else Lane.EXECUTE_EXPORT
    await enqueue(session, lane, {"request_id": str(request_id)}, org_id=org_id)
    return await get_subject_request(
        session, request_kind=request_kind, request_id=request_id, bundle_url=None
    )


async def get_subject_request(
    session: AsyncSession,
    *,
    request_kind: Literal["erasure", "export"],
    request_id: UUID,
    bundle_url: str | None,
) -> ErasureRequestView | ExportRequestView:
    table = "erasure_request" if request_kind == "erasure" else "export_request"
    row = (
        await session.execute(text(f"select * from {table} where id=:id"), {"id": request_id})
    ).mappings().one_or_none()
    if row is None:
        raise not_found()
    if request_kind == "erasure":
        return ErasureRequestView.model_validate(row)
    payload = dict(row)
    payload["bundle_url"] = bundle_url
    return ExportRequestView.model_validate(payload)


async def list_subject_requests(
    session: AsyncSession,
    *,
    request_kind: Literal["erasure", "export"],
    cursor: str | None,
    limit: int,
) -> ErasureRequestPage | ExportRequestPage:
    table = "erasure_request" if request_kind == "erasure" else "export_request"
    position = _RequestCursor.decode(cursor) if cursor else None
    rows = (
        await session.execute(
            text(
                f"select * from {table} where (cast(:at as timestamptz) is null or "
                "(requested_at,id)<(cast(:at as timestamptz),cast(:id as uuid))) "
                "order by requested_at desc,id desc limit :limit"
            ),
            {
                "at": position.requested_at if position else None,
                "id": position.request_id if position else None,
                "limit": limit + 1,
            },
        )
    ).mappings().all()
    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = (
        _RequestCursor(page[-1]["requested_at"], page[-1]["id"]).encode()
        if has_more and page
        else None
    )
    pagination = CursorPage(next_cursor=next_cursor, has_more=has_more)
    if request_kind == "erasure":
        return ErasureRequestPage(
            data=[ErasureRequestView.model_validate(row) for row in page],
            pagination=pagination,
        )
    return ExportRequestPage(
        data=[ExportRequestView.model_validate({**dict(row), "bundle_url": None}) for row in page],
        pagination=pagination,
    )
