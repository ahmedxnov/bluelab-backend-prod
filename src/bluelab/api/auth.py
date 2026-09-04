"""The Auth surface — sign in, read the current session, sign out.

Paths, `operationId`s and status codes are `api/openapi.yaml`'s. The conformance
diff fails the build on any divergence (ADR-0035), so this file conforms to the
contract rather than describing it.

## Why this lives in `bluelab.api` and not in `modules/identity`

Every other route group belongs to its module. This one cannot, and the reason is
the layering contract rather than taste.

A route needs `bluelab.api.deps` — for the resolved principal, the session store,
and the scope tuple. `bluelab.modules` sits *below* `bluelab.api` in the layers
contract, so a router inside `modules/identity` importing `deps` is an upward
import and a build failure. The alternative — pushing `deps` down into
`platform` — would put session resolution, and therefore the gate check, beneath
the layer that owns request handling, which is worse: the gate would become
something any module could bypass by not calling it.

So the Auth *routes* live here and the Auth *behaviour* stays in
`modules.identity.service`. This file holds no decisions: it turns results into
responses and nothing else.

## Sign-in opens a transaction that can read nothing

`ScopeContext.anonymous()`, not a privileged session — `platform.db.session`
exposes no unscoped session at all, and the one escape hatch that exists is barred
from request paths by an import-linter contract. What makes the lookup possible is
the single enumerated definer helper `app_account_for_sign_in`, which answers one
question about one email and returns seven columns. Everything else that
transaction could touch reads zero rows.

## The cookie is written in exactly one place

Through `set_session_cookie` / `clear_session_cookie`, never
`response.set_cookie`. `tools/audit_cookie_attributes.py` enforces that as a build
gate: the startup audit can only be total if every cookie the application can emit
passes through the one function that audits it (SEC-002).
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from bluelab.api.deps import (
    ClientAddress,
    GatedPrincipal,
    SessionStoreDep,
    ThrottleDep,
    scope_of,
    session_cookie,
)
from bluelab.modules.identity import service
from bluelab.modules.identity.gates import pending_gates
from bluelab.modules.identity.schemas import (
    AcceptancesRequest,
    FirstSignInRequest,
    Gate,
    SessionView,
    SignInRequest,
)
from bluelab.platform.config import Settings, get_settings
from bluelab.platform.db.scope import ScopeContext
from bluelab.platform.db.session import scoped_transaction
from bluelab.platform.errors import catalog
from bluelab.platform.errors.denial import ProblemError
from bluelab.platform.security.cookies import (
    SESSION_COOKIE,
    CookieSpec,
    clear_session_cookie,
    session_spec,
    set_session_cookie,
)
from bluelab.platform.security.sessions import SessionStore

router = APIRouter(tags=["Auth"])

SettingsDep = Annotated[Settings, Depends(get_settings)]


def _spec(settings: Settings) -> CookieSpec:
    """The session cookie's attributes, audited on construction.

    Built per request rather than once at import so `cookie_secure` follows the
    settings the process actually loaded. `CookieSpec.__post_init__` runs the
    SEC-002 audit, so a misconfigured deployment fails here rather than serving one
    insecure cookie per sign-in.
    """
    return session_spec(
        SESSION_COOKIE,
        max_age=settings.session_absolute_seconds,
        secure=settings.cookie_secure,
    )


@router.post(
    "/auth/session",
    operation_id="signIn",
    response_model=SessionView,
    status_code=status.HTTP_200_OK,
    summary="Sign in",
)
async def sign_in(
    payload: SignInRequest,
    response: Response,
    store: SessionStoreDep,
    throttle: ThrottleDep,
    source: ClientAddress,
    settings: SettingsDep,
) -> SessionView:
    """Verify a credential, open a session, set the cookie.

    An account still holding its provisioned credential gets `200` and a real
    session — a gate-limited one. That is not an error: they authenticated
    correctly, and `pending_gates` in the body is how the client knows to route
    them to the first-sign-in screen (FR-IDA-004).

    The response body is built under the account's own scope, so the display name
    and org come back through `account_self_read` and `org_member_read` rather
    than through the definer helper. The helper returns only what authentication
    needs, and nothing it returns is rendered.

    That read happens *before* the session is minted — see the comment on the
    ordering below. The scope it runs under is derived from the authenticated
    principal, not from a session record, so it does not need one to exist.
    """
    # Before the transaction, and before argon2id. The 19 MiB a verification
    # allocates is the thing being rationed, and it is spent inside `authenticate`
    # for an unknown email as readily as a known one.
    await service.guard_sign_in(throttle, settings, email=payload.email, source=source)

    async with scoped_transaction(ScopeContext.anonymous()) as db:
        authenticated = await service.authenticate(
            db, email=payload.email, password=payload.password
        )

    # The body is built BEFORE the session exists, and the ordering matters.
    #
    # This scope comes entirely from `authenticated` — no field of it is read back
    # off the session record — so nothing here needs the session to have been
    # created. Minting first would mean that a failure in this block (a
    # deactivation racing the sign-in, so `load_principal` raises `session-invalid`;
    # a role outside the contract's enum, so `view` raises) answers the client with
    # an error while leaving them holding a live cookie for a live Valkey record.
    #
    # Built in this order, every failure path leaves no session behind.
    scope = ScopeContext.account(
        org_id=authenticated.org_id,
        team_id=authenticated.team_id,
        account_id=authenticated.account_id,
        role=service.role_of(authenticated.role),
    )
    async with scoped_transaction(scope) as db:
        account, org = await service.load_principal(db, authenticated.account_id)
        gates = (authenticated.gate,) if authenticated.gate else await pending_gates(db, account)
        view = service.view(account, org, gates)

    raw = await store.create(
        account_id=authenticated.account_id,
        org_id=authenticated.org_id,
        team_id=authenticated.team_id,
        role=authenticated.role,
        # `gates[0]`, not `authenticated.gate`. The latter is derived from
        # `credential_state` alone, so it is `first_sign_in` or nothing — which
        # meant a session opened by an account owing consent or terms was stored
        # with NO gate, sailed past `current_principal`, and reached the whole
        # product surface while the response body politely reported the gate the
        # client was free to ignore. CMP-002/CMP-005 are not advisory.
        #
        # `pending_gates` returns them in precedence order and `SessionRecord.gate`
        # holds one value, so the first is the one that limits the session.
        gate=gates[0] if gates else None,
    )
    set_session_cookie(response, _spec(settings), raw)
    # Last, once the sign-in has actually succeeded. The counters then measure
    # consecutive failures rather than traffic.
    await service.clear_sign_in_throttle(throttle, email=payload.email, source=source)
    return view


@router.get(
    "/auth/session",
    operation_id="getSession",
    response_model=SessionView,
    summary="Current session and pending gates",
)
async def get_session(record: GatedPrincipal) -> SessionView:
    """The current principal, re-read from the database.

    Takes `GatedPrincipal`, not `CurrentPrincipal`: this is the endpoint a
    gate-limited client calls to discover *which* gate it is behind. Refusing it
    with the very `409` the client is trying to understand would deadlock them on
    the sign-in screen.
    """
    async with scoped_transaction(scope_of(record)) as db:
        account, org = await service.load_principal(db, UUID(record.account_id))
        gates = (record.gate,) if record.gate else await pending_gates(db, account)
        return service.view(account, org, gates)  # type: ignore[arg-type]


async def _rotate(store: SessionStore, raw: str, *, gate: Gate | None) -> str:
    """Re-issue the session id at its new gate level.

    Raises:
        ProblemError: `401 session-invalid` if the session vanished mid-request —
            a deactivation revoking it, or the absolute clock expiring between the
            gate check and here.

    Rotation is required, not tidiness: clearing a gate is an authentication-level
    change, and ASVS 3.2.1 / OWASP A07 call for a new identifier at that point.
    Any value captured before it — a shoulder-surfed cookie, a proxy log from the
    provisioning email — must not still open the now less-limited session.
    """
    rotated = await store.set_gate_and_rotate(raw, gate=gate)
    if rotated is None:
        raise ProblemError(catalog.SESSION_INVALID)
    return rotated


@router.post(
    "/auth/first-sign-in",
    operation_id="completeFirstSignIn",
    response_model=SessionView,
    status_code=status.HTTP_200_OK,
    summary="Complete the first-sign-in gate",
)
async def complete_first_sign_in(
    payload: FirstSignInRequest,
    response: Response,
    record: GatedPrincipal,
    store: SessionStoreDep,
    throttle: ThrottleDep,
    settings: SettingsDep,
    raw: Annotated[str, Depends(session_cookie)],
) -> SessionView:
    """Set the real password, record consent and terms, then lift the gate.

    Takes `GatedPrincipal` — this is one of the two endpoints a limited session is
    allowed to reach, and refusing it with the `409` it exists to clear would trap
    the user on the first-sign-in screen.

    The three writes and the gate lift are one transaction; the cookie is replaced
    only after it commits. A failure anywhere leaves the account exactly as it was,
    still behind the gate, still holding the provisioned credential.
    """
    # Before the transaction and before argon2id, for the same reason sign-in is
    # throttled before it: the hash is spent whether or not the gate is pending.
    await service.guard_gate_attempt(throttle, settings, account_id=record.account_id)

    scope = scope_of(record)
    async with scoped_transaction(scope) as db:
        await service.complete_first_sign_in(
            db,
            new_password=payload.new_password,
            consent=payload.consent,
            terms_accepted=payload.terms_accepted,
        )
        account, org = await service.load_principal(db, UUID(record.account_id))
        gates = await pending_gates(db, account)
        view = service.view(account, org, gates)

    rotated = await _rotate(store, raw, gate=gates[0] if gates else None)
    set_session_cookie(response, _spec(settings), rotated)
    return view


@router.post(
    "/auth/acceptances",
    operation_id="recordAcceptances",
    response_model=SessionView,
    status_code=status.HTTP_200_OK,
    summary="Record re-consent or re-acceptance after a material document change",
)
async def record_acceptances(
    payload: AcceptancesRequest,
    response: Response,
    record: GatedPrincipal,
    store: SessionStoreDep,
    throttle: ThrottleDep,
    settings: SettingsDep,
    raw: Annotated[str, Depends(session_cookie)],
) -> SessionView:
    """Clear whichever consent or terms gate is pending, and only that one.

    The session's new gate is **recomputed**, not cleared. An account behind both
    instruments that supplies only consent still owes terms, and setting the gate
    to None there would hand it the whole product surface while the response body
    honestly reported `pending_gates: ["terms"]`.
    """
    await service.guard_gate_attempt(throttle, settings, account_id=record.account_id)

    async with scoped_transaction(scope_of(record)) as db:
        await service.record_acceptances(
            db, consent=payload.consent, terms_accepted=payload.terms_accepted
        )
        account, org = await service.load_principal(db, UUID(record.account_id))
        gates = await pending_gates(db, account)
        view = service.view(account, org, gates)

    rotated = await _rotate(store, raw, gate=gates[0] if gates else None)
    set_session_cookie(response, _spec(settings), rotated)
    return view


@router.delete(
    "/auth/session",
    operation_id="signOut",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Sign out",
)
async def sign_out(
    response: Response,
    store: SessionStoreDep,
    settings: SettingsDep,
    raw: Annotated[str, Depends(session_cookie)],
) -> None:
    """End the session server-side, then clear the cookie.

    Server-side first, and the ordering is the point: deleting the record in
    Valkey is what actually ends the session. Clearing the cookie is courtesy to
    the browser — a client that ignores `Set-Cookie`, or an attacker who already
    copied the value, is stopped by the record being gone and by nothing else
    (ADR-0028).

    Takes the raw cookie rather than a resolved principal, so signing out of an
    already-expired session still succeeds. Answering `401` there would leave a
    user staring at a sign-out button that does not work.
    """
    await store.revoke(raw)
    clear_session_cookie(response, _spec(settings))
