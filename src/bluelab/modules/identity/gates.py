"""Derive legal gates from current evidence on every authenticated request.

First sign-in subsumes both instruments. Missing documents keep their gates
closed; future versions do not take effect early (data/01 §1). Consent and
terms/privacy acceptance are separate instruments (CMP-002 / CMP-005).
"""

from __future__ import annotations

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.modules.identity.models import (
    CREDENTIAL_INITIAL,
    Account,
    LegalDocumentVersion,
)
from bluelab.modules.identity.schemas import Gate

KIND_NOTICE = "recording_consent_notice"
KIND_TERMS = "terms_of_use"
KIND_PRIVACY = "privacy_notice"


async def current_versions(session: AsyncSession, *kinds: str) -> dict[str, str]:
    """Newest effective versions, with an immutable id tie-break for equal times.

    Missing kinds are deliberately absent. Callers must refuse to record an
    instrument whose required version is missing, never silently skip it.
    """
    rows = await session.execute(
        select(LegalDocumentVersion.kind, LegalDocumentVersion.version)
        .where(
            LegalDocumentVersion.kind.in_(kinds),
            LegalDocumentVersion.effective_at <= func.now(),
        )
        .distinct(LegalDocumentVersion.kind)
        .order_by(
            LegalDocumentVersion.kind,
            LegalDocumentVersion.effective_at.desc(),
            LegalDocumentVersion.id.desc(),
        )
    )
    return {kind: version for kind, version in rows}


# Evidence stays ops-readable. Enumerated definer helpers expose booleans only.
_HAS_ACCEPTED = text("select app_account_has_accepted(:account, :kind, :version)")
_HAS_ACCEPTED_TERMS = text(
    "select app_account_has_accepted_terms(:account, :terms, :privacy)"
)


async def pending_gates(session: AsyncSession, account: Account) -> tuple[Gate, ...]:
    """Current blocking gates, in first-sign-in/consent/terms precedence order."""
    if account.credential_state == CREDENTIAL_INITIAL:
        return ("first_sign_in",)
    versions = await current_versions(session, KIND_NOTICE, KIND_TERMS, KIND_PRIVACY)
    gates: list[Gate] = []
    notice = versions.get(KIND_NOTICE)
    if (
        notice is None
        or not (
            await session.execute(
                _HAS_ACCEPTED,
                {"account": account.id, "kind": KIND_NOTICE, "version": notice},
            )
        ).scalar_one()
    ):
        gates.append("consent")
    terms = versions.get(KIND_TERMS)
    privacy = versions.get(KIND_PRIVACY)
    if (
        terms is None
        or privacy is None
        or not (
            await session.execute(
                _HAS_ACCEPTED_TERMS,
                {"account": account.id, "terms": terms, "privacy": privacy},
            )
        ).scalar_one()
    ):
        gates.append("terms")
    return tuple(gates)
