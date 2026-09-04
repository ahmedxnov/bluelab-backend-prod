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
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bluelab.modules.identity.gates import KIND_NOTICE, KIND_TERMS, current_versions
from bluelab.modules.identity.models import CREDENTIAL_INITIAL, Account, Org
from bluelab.modules.identity.schemas import Gate, OrgView, SessionView
from bluelab.platform.config import Settings
from bluelab.platform.db.scope import Role
from bluelab.platform.errors import catalog
from bluelab.platform.errors.denial import ProblemError
from bluelab.platform.ids import new_id
from bluelab.platform.security import passwords
from bluelab.platform.security.throttle import Limit, Throttle

STATUS_ACTIVE = "active"


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

    Carries ids only. The display name, org name and timezone the response needs
    are read *after* the session exists, through the account's own scope — see
    `load_principal`. Nothing here is rendered to the client.
    """

    account_id: UUID
    org_id: UUID
    team_id: UUID
    role: str
    gate: Gate | None


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
    window = settings.auth_throttle_window_seconds
    identifier = Limit(settings.auth_throttle_identifier_attempts, window)
    per_source = Limit(settings.auth_throttle_source_attempts, window)

    retry_identifier = await throttle.hit(IDENTIFIER_BUCKET, email.strip().lower(), identifier)
    retry_source = await throttle.hit(SOURCE_BUCKET, source, per_source)

    retry = retry_identifier if retry_identifier is not None else retry_source
    if retry is not None:
        raise ProblemError(catalog.RATE_LIMITED, headers={"Retry-After": str(retry)})


async def clear_sign_in_throttle(throttle: Throttle, *, email: str, source: str) -> None:
    """Forget the attempts behind a sign-in that succeeded.

    Both counters, so they measure consecutive failures rather than lifetime
    traffic — otherwise an office behind one address would trip the source limit
    on an ordinary morning, and anyone who fumbles a password once would carry it
    for the rest of the window.
    """
    await throttle.clear(IDENTIFIER_BUCKET, email.strip().lower())
    await throttle.clear(SOURCE_BUCKET, source)


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

    A kind with no published version is SKIPPED, not an error. A fresh
    environment has no notice and no terms, and an account cannot consent to a
    document that does not exist; treating that as a failure would make the
    first-sign-in gate uncompletable on a new deployment.

    `terms_acceptance` carries both versions because the acceptance covers both
    documents (CMP-005), so it needs the notice version too — and is skipped
    unless both are published.
    """
    versions = await current_versions(session, KIND_NOTICE, KIND_TERMS)
    notice = versions.get(KIND_NOTICE)
    terms_version = versions.get(KIND_TERMS)

    if consent and notice is not None:
        await session.execute(_RECORD_CONSENT, {"id": new_id(), "version": notice})

    if terms and terms_version is not None and notice is not None:
        await session.execute(
            _RECORD_TERMS, {"id": new_id(), "terms": terms_version, "privacy": notice}
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


def view(account: Account, org: Org, gates: tuple[Gate, ...]) -> SessionView:
    """Render the principal as the contract's session body.

    Args:
        account: The signed-in account.
        org: Its org.
        gates: Gates currently blocking full access.

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
    )


async def load_principal(session: AsyncSession, account_id: UUID) -> tuple[Account, Org]:
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
    found = (
        await session.execute(
            select(Account, Org).join(Org, Org.id == Account.org_id).where(Account.id == account_id)
        )
    ).one_or_none()

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
