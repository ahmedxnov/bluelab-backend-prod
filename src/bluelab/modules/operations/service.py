"""Domain services — this module's behaviour, and its published interface to the
other modules. Cross-module callers enter here; they never touch `models.py`.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime
from typing import NoReturn
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
)
from bluelab.platform.config import Settings
from bluelab.platform.errors import catalog
from bluelab.platform.errors.denial import ProblemError
from bluelab.platform.ids import new_id
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
