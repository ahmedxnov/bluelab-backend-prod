"""Domain services — this module's behaviour, and its published interface to the
other modules. Cross-module callers enter here; they never touch `models.py`.

Authentication holds the decisions that must not be re-made per route. The router
turns results into responses; every rule about *who gets in and what they may
reach* lives here, because a rule implemented at one of three call sites is a
rule that is wrong at the other two.

## The disclosure rules, which are the whole point

Sign-in has exactly one state it may disclose and several it may not.

**May not:** whether an email is provisioned. Provisioning is operator-mediated
(FR-IDA-001), so a customer org's account list is a meaningful thing to learn, and
`invalid-credentials` is returned identically for an unknown email and a wrong
password (FR-IDA-005). Identical status, identical body — and identical *timing*,
which is why the unknown-email branch still burns an argon2 verification through
`dummy_verify()`. Returning early there would leak the answer in tens of
milliseconds.

**May:** that an account is deactivated — but only to someone who has proved they
hold its password (AC-IDA-003). `403 account-deactivated` after a *correct*
credential tells a real user why they cannot get in; before one it is a free
oracle. That ordering is the entire control, and it is why verification happens
before the status check rather than after.

## Gate-limited sessions are real sessions

An account still carrying its provisioned initial credential authenticates
successfully and receives a session with `gate="first_sign_in"`. It is not a
half-session or a special token: it resolves like any other, and the gate check in
`bluelab.api.deps` refuses everything except the endpoints that clear it
(FR-IDA-004). Modelling it as a real session with a flag is what stops a route
that forgot to check from becoming a bypass — the flag travels with the principal
rather than with the caller's good intentions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.adapters.secrets import DeliverySecretContext, SecretSealer
from bluelab.modules.identity.gates import (
    KIND_NOTICE,
    KIND_PRIVACY,
    KIND_TERMS,
    current_versions,
)
from bluelab.modules.identity.models import (
    CREDENTIAL_INITIAL,
    Account,
    LegalDocumentVersion,
    Org,
    OrgRetentionPolicy,
    OrgServiceTerm,
)
from bluelab.modules.identity.schemas import (
    Gate,
    LegalDocuments,
    LegalDocumentView,
    OrgView,
    SessionView,
)
from bluelab.platform.clock import now
from bluelab.platform.config import Settings
from bluelab.platform.db.scope import Role
from bluelab.platform.errors import catalog
from bluelab.platform.errors.denial import ProblemError
from bluelab.platform.ids import new_id
from bluelab.platform.queue.catalog import Lane
from bluelab.platform.queue.enqueue import enqueue
from bluelab.platform.security import passwords
from bluelab.platform.security.throttle import Limit, Throttle
from bluelab.platform.security.tokens import hash_token, mint_reset_token, mint_token
from bluelab.platform.telemetry import metrics
from bluelab.platform.telemetry.logging import get_logger

STATUS_ACTIVE = "active"

LEGAL_KINDS = (KIND_NOTICE, KIND_TERMS, KIND_PRIVACY)

_auth_log = get_logger("bluelab.auth")


_SIGN_IN_LOOKUP = text(
    "select id, org_id, team_id, role, password_hash, status, credential_state"
    "  from app_account_for_sign_in(:email)"
)
"""The one enumerated definer helper reachable from a request path.

Sign-in runs under `ScopeContext.anonymous()` — a transaction that reads zero
rows from every customer table — so this function, not the scope, is what makes
the lookup possible. See its comment in `tools/generate_rls_policies.py`.
"""


@dataclass(frozen=True, slots=True)
class Authenticated:
    """A verified principal, before a session exists for it.

    The credential hash is carried only long enough to ensure a concurrent reset
    did not replace the credential between verification and session issue. The
    display name, org name and timezone are read through the account's own scope.
    Nothing here is rendered to the client.
    """

    account_id: UUID
    org_id: UUID
    team_id: UUID
    role: str
    gate: Gate | None
    password_hash: str


IDENTIFIER_BUCKET = "identifier"
SOURCE_BUCKET = "source"


async def guard_sign_in(throttle: Throttle, settings: Settings, *, email: str, source: str) -> None:
    """Refuse an attempt that is past either throttle window (SEC-004/005).

    Args:
        throttle: Counters over the coordination store.
        settings: Supplies both limits and the shared window.
        email: As supplied. Hashed before it becomes a key — see `Throttle`.
        source: The caller's peer address.

    Raises:
        ProblemError: `429 rate-limited`, carrying `Retry-After`.

    **Called before the credential is verified, and that is the whole point.** The
    cost this bounds is argon2id's 19 MiB, which is spent inside `authenticate` —
    including for an unknown email, because `dummy_verify` must run to keep the
    timing uniform. Checking afterwards would refuse the response while still
    paying for it, which stops credential stuffing and does nothing at all about
    the memory.

    Both counters are hit on every attempt, even one already over the other's
    limit, so a caller cannot spend a cheap identifier refusal to avoid being
    counted against their source. The identifier is counted first only so that the
    narrower limit names itself in the response.

    Nothing here varies with whether the account exists. The counters are keyed on
    what was *submitted*, so an unknown address throttles exactly like a known one
    and `429` discloses nothing `401` did not (FR-IDA-005).
    """
    normalized = email.strip().lower()
    retry_source = await throttle.hit(
        SOURCE_BUCKET,
        source,
        Limit(
            settings.auth_throttle_source_attempts,
            settings.auth_throttle_source_window_seconds,
        ),
    )
    retry_identifier = await throttle.backoff_remaining(IDENTIFIER_BUCKET, normalized)
    retry = retry_identifier if retry_identifier is not None else retry_source
    if retry is not None:
        _record_auth_signal(
            metrics.AuthSignal.THROTTLE_TRIPPED,
            identifier=normalized,
            source=source,
        )
        raise ProblemError(catalog.RATE_LIMITED, headers={"Retry-After": str(retry)})


async def record_sign_in_failure(
    throttle: Throttle, settings: Settings, *, email: str, source: str
) -> None:
    """Record one invalid proof and apply bounded exponential backoff."""
    normalized = email.strip().lower()
    await throttle.record_failure(
        IDENTIFIER_BUCKET,
        normalized,
        threshold=settings.auth_throttle_identifier_attempts,
        window_seconds=settings.auth_throttle_window_seconds,
        base_seconds=settings.auth_backoff_base_seconds,
        max_seconds=settings.auth_backoff_max_seconds,
    )
    _record_auth_signal(
        metrics.AuthSignal.SIGN_IN_FAILED,
        identifier=normalized,
        source=source,
    )


def _record_auth_signal(
    signal: metrics.AuthSignal, *, identifier: str, source: str
) -> None:
    """Emit a content-free, stable-identity security signal."""
    metrics.record_auth_signal(signal)
    _auth_log.warning(
        "auth_signal",
        signal=signal.value,
        identifier_id=hash_token(identifier),
        source_id=hash_token(source),
    )


async def clear_sign_in_throttle(throttle: Throttle, *, email: str, source: str) -> None:
    """Forget the attempts behind a sign-in that succeeded.

    Both counters, so they measure consecutive failures rather than lifetime
    traffic — otherwise an office behind one address would trip the source limit
    on an ordinary morning, and anyone who fumbles a password once would carry it
    for the rest of the window.
    """
    await throttle.clear_failures(IDENTIFIER_BUCKET, email.strip().lower())
    await throttle.clear(SOURCE_BUCKET, source)


async def guard_password_reset_request(
    throttle: Throttle, settings: Settings, *, email: str, source: str
) -> None:
    """Apply the non-enumerating 3/email/hour and 10/source/hour limits."""
    limit_window = settings.password_reset_request_window_seconds
    retries = (
        await throttle.hit(
            "reset_request_identifier",
            email.strip().lower(),
            Limit(settings.password_reset_request_identifier_attempts, limit_window),
        ),
        await throttle.hit(
            "reset_request_source",
            source,
            Limit(settings.password_reset_request_source_attempts, limit_window),
        ),
    )
    retry = next((value for value in retries if value is not None), None)
    if retry is not None:
        _record_auth_signal(
            metrics.AuthSignal.THROTTLE_TRIPPED,
            identifier=email.strip().lower(),
            source=source,
        )
        raise ProblemError(catalog.RATE_LIMITED, headers={"Retry-After": str(retry)})


async def guard_password_reset_completion(
    throttle: Throttle, settings: Settings, *, source: str
) -> None:
    """Apply the reset-token completion source limit."""
    retry = await throttle.hit(
        "reset_complete_source",
        source,
        Limit(
            settings.password_reset_complete_source_attempts,
            settings.password_reset_complete_window_seconds,
        ),
    )
    if retry is not None:
        _record_auth_signal(
            metrics.AuthSignal.THROTTLE_TRIPPED,
            identifier="password-reset-completion",
            source=source,
        )
        raise ProblemError(catalog.RATE_LIMITED, headers={"Retry-After": str(retry)})


async def authenticate(session: AsyncSession, *, email: str, password: str) -> Authenticated:
    """Verify a credential and resolve the principal behind it.

    Args:
        session: An **anonymous** scoped transaction (`ScopeContext.anonymous()`).
            Sign-in runs before a principal exists, so there is nothing to scope
            to; the lookup goes through `app_account_for_sign_in` instead, and
            everything else this session could touch reads zero rows.
        email: As supplied. The helper lowercases it; provisioning stores it that
            way.
        password: As supplied. Never logged, never returned.

    Returns:
        The verified principal's ids and the gate a new session must carry.

    Raises:
        ProblemError: `401 invalid-credentials` for an unknown email OR a wrong
            password, indistinguishably. `403 account-deactivated` only after a
            correct credential on a deactivated account (AC-IDA-003).
    """
    row = (await session.execute(_SIGN_IN_LOOKUP, {"email": email})).one_or_none()

    if row is None:
        # Burn the verification cost anyway. Without this, the unknown-email path
        # returns in microseconds and the wrong-password path in tens of
        # milliseconds — a user-enumeration oracle readable over the network
        # (SEC-005).
        passwords.dummy_verify()
        raise ProblemError(catalog.INVALID_CREDENTIALS)

    if not passwords.verify_password(password, row.password_hash):
        raise ProblemError(catalog.INVALID_CREDENTIALS)

    # ONLY after the credential is proven. The ordering IS the control: moving
    # this above the verification would disclose account status to anyone who can
    # guess an email address (AC-IDA-003).
    if row.status != STATUS_ACTIVE:
        raise ProblemError(catalog.ACCOUNT_DEACTIVATED)

    return Authenticated(
        account_id=row.id,
        org_id=row.org_id,
        team_id=row.team_id,
        role=row.role,
        # The only gate determinable here. The other two need reads this scope
        # cannot make; `gates.pending_gates` raises rather than guessing, and
        # runs under the account's own scope once the session exists.
        gate="first_sign_in" if row.credential_state == CREDENTIAL_INITIAL else None,
        password_hash=row.password_hash,
    )


_RECORD_CONSENT = text("select app_record_consent(:id, :version)")
_RECORD_TERMS = text("select app_record_terms_acceptance(:id, :terms, :privacy)")
_SET_CREDENTIAL = text("select app_set_initial_credential(:password_hash)")
"""The three enumerated definer helpers the gate exits write through.

`consent_record` and `terms_acceptance` are P9_OPS/system_write_only and
P2_ACCOUNT grants no self-UPDATE, so none of these three writes is reachable
under the account's own scope. Each helper takes its subject from the transaction
GUC — there is no account parameter — so this module cannot record consent, or
set a password, for anybody but the caller. See `tools/generate_rls_policies.py`.
"""


def _require_accepted(*, consent: bool | None, terms_accepted: bool | None) -> None:
    """Refuse an explicit refusal, before anything is written.

    `False` is not a way through a gate. Recording nothing and answering `200`
    would leave a client that had deliberately declined wondering why it is still
    limited; recording something would enter a consent it never gave into the
    evidence trail. `None` is different and legal — it means "not supplied", which
    `POST /auth/acceptances` uses to send only the pending instrument.

    Raises:
        ProblemError: `422 validation-error` (AC-IDA-002 — and it records nothing,
            which is the half of that criterion that fails silently).
    """
    declined = [
        name
        for name, value in (("consent", consent), ("terms_accepted", terms_accepted))
        if value is False
    ]
    if declined:
        raise ProblemError(
            catalog.VALIDATION_ERROR,
            detail=f"{', '.join(declined)} must be true to clear the gate",
        )


GATE_BUCKET = "gate"


async def guard_gate_attempt(
    throttle: Throttle, settings: Settings, *, account_id: str
) -> None:
    """Bound how often one account may attempt a gate exit (SEC-004).

    Args:
        throttle: Counters over the coordination store.
        settings: Supplies the limit and the shared window.
        account_id: From the session record, never from the request.

    Raises:
        ProblemError: `429 rate-limited`, carrying `Retry-After`.

    `POST /auth/first-sign-in` hashes with argon2id — 19 MiB per call — and it does
    so BEFORE it can know the gate is still pending, because
    `app_set_initial_credential` is one atomic check-and-set and splitting it to
    check first would introduce a TOCTOU. So every call costs a full hash even when
    the answer is `409`, and measured at 23 ms apiece an authenticated caller could
    spend our memory without limit. Both operations declare `429` in the contract;
    nothing was enforcing it.

    Keyed on the ACCOUNT, not the source. Reaching either endpoint already requires
    a session, and a session already required a sign-in through the per-source
    counter — so the account is the identity that has not yet been bounded, and it
    is the one a compromised session is pinned to.
    """
    retry = await throttle.hit(
        GATE_BUCKET,
        account_id,
        Limit(settings.auth_throttle_identifier_attempts, settings.auth_throttle_window_seconds),
    )
    if retry is not None:
        raise ProblemError(catalog.RATE_LIMITED, headers={"Retry-After": str(retry)})


async def _record_instruments(
    session: AsyncSession, *, consent: bool, terms: bool
) -> None:
    """Write the requested instruments at the versions currently published.

    The versions come from `legal_document_version`, never from the request. A
    client cannot nominate what it is consenting to — and the helpers reject an
    unpublished version anyway, so a request-sourced value would surface as a 500
    rather than as quiet corruption.

    Validate every requested instrument before writing anything. A missing
    effective document leaves the corresponding gate pending (CMP-002/CMP-005).
    Consent names its own notice; terms acceptance names the terms/privacy pair.
    """
    versions = await current_versions(session, KIND_NOTICE, KIND_TERMS, KIND_PRIVACY)
    notice = versions.get(KIND_NOTICE)
    terms_version = versions.get(KIND_TERMS)
    privacy = versions.get(KIND_PRIVACY)

    if consent and notice is None:
        raise ProblemError(catalog.CONSENT_REQUIRED)
    if terms and (terms_version is None or privacy is None):
        raise ProblemError(catalog.TERMS_ACCEPTANCE_REQUIRED)

    if consent and notice is not None:
        await session.execute(_RECORD_CONSENT, {"id": new_id(), "version": notice})

    if terms:
        await session.execute(
            _RECORD_TERMS, {"id": new_id(), "terms": terms_version, "privacy": privacy}
        )


async def complete_first_sign_in(
    session: AsyncSession,
    *,
    new_password: str,
    consent: bool,
    terms_accepted: bool,
) -> None:
    """Clear the first-sign-in gate — all three actions, or none of them.

    Args:
        session: A transaction scoped to the account completing the gate.
        new_password: Already length-bounded by `FirstSignInRequest`.
        consent: Must be true (CMP-002).
        terms_accepted: Must be true (CMP-005).

    Raises:
        ProblemError: `422 validation-error` if either instrument is declined;
            `409 first-sign-in-not-pending` if the credential is already set.

    **The credential flips LAST, and that ordering is the control.** FR-IDA-004
    says completing any subset alone does not grant access, and the credential is
    what grants it — `credential_state = 'set'` is what closes the gate. Recording
    the instruments first means every failure path leaves the gate shut: the 409
    below raises inside the caller's transaction, so the consent and terms rows
    roll back with it and the account is exactly as it was.

    Written the other way round — credential first — a failure after it would
    leave someone admitted with no consent on record, which is the one outcome
    CMP-002 exists to prevent.
    """
    _require_accepted(consent=consent, terms_accepted=terms_accepted)
    await _record_instruments(session, consent=consent, terms=terms_accepted)

    pending = (
        await session.execute(
            _SET_CREDENTIAL, {"password_hash": passwords.hash_password(new_password)}
        )
    ).scalar_one()
    if not pending:
        # The helper updates nothing when `credential_state` is already 'set', so
        # this is the replay-after-success case and the already-admitted case at
        # once (gate F-6). Raising here rolls the instruments back above.
        raise ProblemError(catalog.FIRST_SIGN_IN_NOT_PENDING)


async def record_acceptances(
    session: AsyncSession, *, consent: bool | None, terms_accepted: bool | None
) -> None:
    """Record re-consent or re-acceptance after a material document change.

    Args:
        session: A transaction scoped to the accepting account.
        consent: True to record; None to leave alone (CMP-002).
        terms_accepted: True to record; None to leave alone (CMP-005).

    Raises:
        ProblemError: `422 validation-error` if either is explicitly false.

    Genuinely idempotent, unlike first sign-in: the guard is the unique
    `(person, version)` row and writing it twice changes nothing
    (00-contract-overview §6). Nothing here flips a credential, so there is no
    one-way state to replay into.
    """
    _require_accepted(consent=consent, terms_accepted=terms_accepted)
    await _record_instruments(session, consent=bool(consent), terms=bool(terms_accepted))


def view(
    account: Account,
    org: Org,
    gates: tuple[Gate, ...],
    *,
    session_expires_at: datetime,
) -> SessionView:
    """Render the principal as the contract's session body.

    Args:
        account: The signed-in account.
        org: Its org.
        gates: Gates currently blocking full access.
        session_expires_at: Effective idle-or-absolute session expiry.

    Returns:
        The `SessionView` both session endpoints answer with.
    """
    return SessionView(
        account_id=account.id,
        display_name=account.display_name,
        email=account.email,
        role=account.role,  # type: ignore[arg-type]
        team_id=account.team_id,
        org=OrgView(id=org.id, name=org.name, timezone=org.timezone),
        pending_gates=list(gates),
        session_expires_at=session_expires_at,
    )


async def load_principal(
    session: AsyncSession,
    account_id: UUID,
) -> tuple[Account, Org]:
    """Re-read the account behind an established session.

    The session record deliberately stores no display name, role label, or org
    name (`SessionRecord`'s own docstring). Every read therefore goes back to the
    database, so a rep moved between teams or renamed by ops sees the change on
    their next request rather than on their next sign-in (FR-IDA-009).

    Raises:
        ProblemError: `401 session-invalid` if the account has vanished or been
            deactivated since the session opened. Deactivation revokes sessions
            directly (FR-IDA-010); this is the backstop for the window between
            the two, and it does not distinguish the two cases.
    """
    statement = (
        select(Account, Org)
        .join(Org, Org.id == Account.org_id)
        .where(Account.id == account_id)
    )
    found = (await session.execute(statement)).one_or_none()

    if found is None or found[0].status != STATUS_ACTIVE:
        raise ProblemError(catalog.SESSION_INVALID)
    return found[0], found[1]


def role_of(role: str) -> Role:
    """The stored role string as the scope enum.

    A separate function so the one place that widens a database string into a
    privilege-bearing value is named and greppable. `Role(role)` raises on an
    unknown value, which is the correct outcome: a role the policy matrix does not
    model must not resolve to a scope at all.
    """
    return Role(role)


async def legal_documents(session: AsyncSession) -> LegalDocuments:
    """Return the three newest effective legal documents, or fail closed."""
    rows = (
        (
            await session.execute(
                select(LegalDocumentVersion)
                .where(
                    LegalDocumentVersion.kind.in_(LEGAL_KINDS),
                    LegalDocumentVersion.effective_at <= func.now(),
                )
                .distinct(LegalDocumentVersion.kind)
                .order_by(
                    LegalDocumentVersion.kind,
                    LegalDocumentVersion.effective_at.desc(),
                    LegalDocumentVersion.id.desc(),
                )
            )
        )
        .scalars()
        .all()
    )
    current = {
        row.kind: LegalDocumentView(
            version=row.version,
            effective_at=row.effective_at,
            url=row.url,
        )
        for row in rows
    }
    if set(current) != set(LEGAL_KINDS):
        raise ProblemError(
            catalog.SERVICE_UNAVAILABLE,
            detail="The current legal-document catalog is unavailable.",
        )
    return LegalDocuments(
        recording_consent_notice=current[KIND_NOTICE],
        terms_of_use=current[KIND_TERMS],
        privacy_notice=current[KIND_PRIVACY],
    )


_RESET_ACCOUNT = text(
    "select account_id, org_id from app_account_for_password_reset(:email)"
)
_ISSUE_RESET = text(
    "select app_issue_password_reset("
    ":account_id, :org_id, :token_id, :token_hash, :expires_at,"
    " :email_send_id, :ciphertext)"
)
_CONSUME_RESET = text(
    "select app_consume_password_reset(:token_hash, :password_hash)"
)


async def issue_password_reset(
    session: AsyncSession,
    *,
    email: str,
    sealer: SecretSealer,
) -> None:
    """Create a reset delivery without revealing whether ``email`` exists."""
    found = (
        await session.execute(_RESET_ACCOUNT, {"email": email.strip().lower()})
    ).one_or_none()

    token = mint_reset_token()
    token_id = new_id()
    email_send_id = new_id()
    # Unknown addresses still pay the same CSPRNG and AEAD work. These synthetic
    # ids are discarded and cannot become a persistence side channel.
    account_id = found.account_id if found is not None else new_id()
    org_id = found.org_id if found is not None else new_id()
    ciphertext = await sealer.seal(
        token.plaintext,
        context=DeliverySecretContext(
            email_send_id=email_send_id,
            org_id=org_id,
            purpose="password_reset_token",
        ),
    )

    if found is None:
        return

    issued = (
        await session.execute(
            _ISSUE_RESET,
            {
                "account_id": account_id,
                "org_id": org_id,
                "token_id": token_id,
                "token_hash": token.token_hash,
                "expires_at": token.expires_at,
                "email_send_id": email_send_id,
                "ciphertext": ciphertext,
            },
        )
    ).scalar_one()
    # A deactivation can win after the lookup. That outcome remains the same
    # unconditional 202 and the transaction persists nothing.
    if not issued:
        return
    await enqueue(
        session,
        Lane.DISPATCH_EMAIL,
        {"email_send_id": str(email_send_id)},
        org_id=org_id,
    )


async def complete_password_reset(
    session: AsyncSession,
    *,
    token: str,
    new_password: str,
) -> UUID:
    """Consume one reset token and return the account whose sessions must end."""
    account_id = (
        await session.execute(
            _CONSUME_RESET,
            {
                "token_hash": hash_token(token),
                "password_hash": passwords.hash_password(new_password),
            },
        )
    ).scalar_one_or_none()
    if account_id is None:
        raise ProblemError(catalog.RESET_TOKEN_INVALID)
    return UUID(str(account_id))


class ProvisionAccountOutcome(StrEnum):
    """Every expected result of the domain-bound provisioning transaction."""

    CREATED = "created"
    ORG_NOT_FOUND = "org_not_found"
    DOMAIN_MISMATCH = "domain_mismatch"
    MANAGER_NOT_FOUND = "manager_not_found"
    DUPLICATE_EMAIL = "duplicate_email"
    INVALID_MANAGER_SHAPE = "invalid_manager_shape"
    INVALID_ROLE = "invalid_role"


async def provision_org(
    session: AsyncSession,
    *,
    name: str,
    registered_domain: str,
    timezone: str,
) -> Org:
    """Create one customer tenancy shell through the identity service boundary."""
    org = Org(
        id=new_id(),
        name=name,
        registered_domain=registered_domain,
        timezone=timezone,
    )
    session.add(org)
    await session.flush()
    return org


async def lifecycle_projection(
    session: AsyncSession, org_id: UUID, *, lock: bool = False
) -> tuple[Org, OrgServiceTerm | None, OrgRetentionPolicy | None] | None:
    """Expose this module's lifecycle projection to authorized ops services."""
    statement = select(Org).where(Org.id == org_id)
    if lock:
        statement = statement.with_for_update()
    org = (await session.execute(statement)).scalar_one_or_none()
    if org is None:
        return None
    term = (
        (await session.execute(
            select(OrgServiceTerm).where(OrgServiceTerm.id == org.current_service_term_id)
        )).scalar_one_or_none()
        if org.current_service_term_id else None
    )
    policy = (await session.execute(
        select(OrgRetentionPolicy).where(OrgRetentionPolicy.org_id == org_id)
    )).scalar_one_or_none()
    return org, term, policy


async def apply_org_service_term(
    session: AsyncSession,
    *,
    org: Org,
    term_id: UUID,
    sequence: int,
    start_on: date,
    last_access_on: date,
    starts_at: datetime,
    ends_at: datetime,
    contract_reference: str,
    retention_policy_reference: str,
    confirmed_by: UUID,
    confirmed_at: datetime,
    reason: str,
    custom_policy: dict[str, Any] | None,
) -> None:
    """Apply one accepted independent decision within the ops audit transaction."""
    session.add(OrgServiceTerm(
        id=term_id, org_id=org.id, lifecycle_sequence=sequence,
        start_on=start_on, last_access_on=last_access_on,
        calendar_timezone=org.timezone, starts_at=starts_at, ends_at=ends_at,
        contract_reference=contract_reference,
        retention_policy_reference=retention_policy_reference,
        confirmed_by=confirmed_by, confirmed_at=confirmed_at, reason=reason,
    ))
    await session.flush()
    org.current_service_term_id = term_id
    org.service_starts_at = starts_at
    org.service_ends_at = ends_at
    org.service_term_enforced = True
    org.lifecycle_sequence = sequence
    org.lifecycle_status = "active"
    org.offboarding_id = None
    org.offboarding_started_at = None
    org.offboarding_started_by = None
    org.purge_eligible_at = None
    org.retention_policy_reference = None
    if custom_policy is not None:
        existing = (await session.execute(
            select(OrgRetentionPolicy).where(OrgRetentionPolicy.org_id == org.id)
        )).scalar_one_or_none()
        values = {
            "policy_reference": str(custom_policy["policy_reference"]),
            "contract_reference": contract_reference,
            "period_value": int(custom_policy["period_value"]),
            "period_unit": str(custom_policy["period_unit"]),
            "calendar_timezone": custom_policy.get("calendar_timezone"),
            "effective_sequence": sequence, "approved_at": confirmed_at,
            "approved_by": confirmed_by, "reason": reason,
        }
        if existing is None:
            session.add(OrgRetentionPolicy(org_id=org.id, **values))
        else:
            for key, value in values.items():
                setattr(existing, key, value)


@dataclass(frozen=True, slots=True)
class ProvisionAccountResult:
    outcome: ProvisionAccountOutcome
    account_id: UUID
    email_send_id: UUID


@dataclass(frozen=True, slots=True)
class E1Recipient:
    """The minimum identity projection the E-1 dispatcher may read."""

    email: str
    display_name: str


_ISSUE_INITIAL_CREDENTIALS = text(
    "select app_issue_initial_credentials("
    ":account_id, :org_id, :email, :display_name, :role, :manager_account_id,"
    " :password_hash, :email_send_id, :ciphertext, :expires_at)"
)


async def provision_account(
    session: AsyncSession,
    *,
    org_id: UUID,
    email: str,
    display_name: str,
    role: str,
    manager_account_id: UUID | None,
    sealer: SecretSealer,
) -> ProvisionAccountResult:
    """Atomically create a domain-bound account, E-1 ledger row, and job."""
    account_id = new_id()
    email_send_id = new_id()
    initial_credential = mint_token()
    expires_at = now() + timedelta(hours=24)
    ciphertext = await sealer.seal(
        initial_credential,
        context=DeliverySecretContext(
            email_send_id=email_send_id,
            org_id=org_id,
            purpose="initial_credential",
        ),
    )
    raw_outcome = (
        await session.execute(
            _ISSUE_INITIAL_CREDENTIALS,
            {
                "account_id": account_id,
                "org_id": org_id,
                "email": email.strip().lower(),
                "display_name": display_name,
                "role": role,
                "manager_account_id": manager_account_id,
                "password_hash": passwords.hash_password(initial_credential),
                "email_send_id": email_send_id,
                "ciphertext": ciphertext,
                "expires_at": expires_at,
            },
        )
    ).scalar_one()
    outcome = ProvisionAccountOutcome(str(raw_outcome))
    if outcome is ProvisionAccountOutcome.CREATED:
        await enqueue(
            session,
            Lane.DISPATCH_EMAIL,
            {"email_send_id": str(email_send_id)},
            org_id=org_id,
        )
    return ProvisionAccountResult(
        outcome=outcome,
        account_id=account_id,
        email_send_id=email_send_id,
    )


async def resolve_e1_recipient(
    session: AsyncSession, *, account_id: UUID, org_id: UUID
) -> E1Recipient | None:
    """Resolve the current active account through identity's service boundary."""
    row = (
        await session.execute(
            select(Account.email, Account.display_name).where(
                Account.id == account_id,
                Account.org_id == org_id,
                Account.status == STATUS_ACTIVE,
            )
        )
    ).one_or_none()
    if row is None:
        return None
    return E1Recipient(email=str(row.email), display_name=str(row.display_name))
