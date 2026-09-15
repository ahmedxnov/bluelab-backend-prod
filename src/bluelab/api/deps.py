"""Request-scoped dependencies: principal resolution into the scope tuple, the
scoped session, the gate checks, and `Idempotency-Key` handling.

Three principal types, three credentials (api/00 §3):

    account holder   `__Host-bluelab_session` cookie      -> (org, team, role, account)
    candidate        `Authorization: Bearer <token>`      -> (org, team, position, candidate)
    BlueLab ops      `__Host-bluelab_ops_session` cookie  -> ops actor id, no customer scope

The candidate token travels in the invite link's URL **fragment** and is extracted
client-side — it never appears in a path, a query string, or a server log.

## The gate check is opt-out, not opt-in

`CurrentPrincipal` refuses a gate-limited session. The endpoints that *clear* a
gate ask for `GatedPrincipal` instead, which tolerates one.

That direction matters and is the reason this is not a decorator. If admitting a
limited session were the default and routes opted into checking, then every route
added later would admit one until someone remembered — and the failure would be
silent, because a gate-limited session is a valid session that reads and writes
perfectly well. Defaulting to refusal means a forgotten route fails closed:
`409 first-sign-in-required` for a user who should have seen exactly that
(FR-IDA-004, OWASP A01 deny-by-default).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, Request
from sqlalchemy import text
from valkey.asyncio import Valkey

from bluelab.adapters.generation_llm import (
    GenerationProvider,
    create_generation_provider,
)
from bluelab.adapters.object_store import ObjectStore, create_object_store
from bluelab.adapters.secrets import (
    KmsSealer,
    KmsUnsealer,
    LocalAeadCipher,
    SecretSealer,
    SecretUnsealer,
    kms_circuit,
    kms_client,
    kms_policy,
)
from bluelab.modules.identity import service
from bluelab.modules.identity.gates import pending_gates
from bluelab.modules.operations import service as operations_service
from bluelab.platform.config import Settings, get_settings
from bluelab.platform.db.privileged import ops_scope
from bluelab.platform.db.scope import Role, ScopeContext
from bluelab.platform.db.session import scoped_transaction
from bluelab.platform.errors import catalog
from bluelab.platform.errors.denial import ProblemError, not_found
from bluelab.platform.resilience import DependencyName, DependencyUnavailable
from bluelab.platform.security.cookies import OPS_SESSION_COOKIE, SESSION_COOKIE
from bluelab.platform.security.sessions import (
    OpsSessionRecord,
    OpsSessionStore,
    SessionRecord,
    SessionStore,
)
from bluelab.platform.security.throttle import Throttle
from bluelab.platform.security.tokens import (
    CandidateBinding,
    StoredCandidateToken,
    TokenInvalid,
    TokenOutcome,
    hash_token,
    verify_candidate_token,
)
from bluelab.platform.security.totp import TotpReplayStore
from bluelab.platform.telemetry import metrics

GATE_PROBLEMS = {
    "first_sign_in": catalog.FIRST_SIGN_IN_REQUIRED,
    "consent": catalog.CONSENT_REQUIRED,
    "terms": catalog.TERMS_ACCEPTANCE_REQUIRED,
}
"""Which problem a limited session answers with. Each is a distinct type because
the client routes to a different screen for each (ux/05 §3)."""


def get_valkey(request: Request) -> Valkey:
    """The coordination client, built once at startup and held on app state.

    A per-request client would open a connection per request — the pool exists to
    be shared, and exhausting it under load is a self-inflicted outage.
    """
    client: Valkey = request.app.state.valkey
    return client


def get_session_store(
    valkey: Annotated[Valkey, Depends(get_valkey)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> SessionStore:
    return SessionStore(
        valkey,
        idle_seconds=settings.session_idle_seconds,
        absolute_seconds=settings.session_absolute_seconds,
    )


def get_ops_session_store(
    valkey: Annotated[Valkey, Depends(get_valkey)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> OpsSessionStore:
    return OpsSessionStore(
        valkey,
        idle_seconds=settings.session_idle_seconds,
        absolute_seconds=settings.session_absolute_seconds,
    )


def get_throttle(valkey: Annotated[Valkey, Depends(get_valkey)]) -> Throttle:
    return Throttle(valkey)


ValkeyDep = Annotated[Valkey, Depends(get_valkey)]
SessionStoreDep = Annotated[SessionStore, Depends(get_session_store)]
OpsSessionStoreDep = Annotated[OpsSessionStore, Depends(get_ops_session_store)]
ThrottleDep = Annotated[Throttle, Depends(get_throttle)]


def object_store_from_request(request: Request, settings: Settings) -> ObjectStore:
    """Return the process-wide object-store adapter."""
    configured: ObjectStore | None = getattr(request.app.state, "object_store", None)
    if configured is None:
        try:
            configured = create_object_store(settings)
        except DependencyUnavailable:
            raise
        except Exception as exc:
            raise DependencyUnavailable(DependencyName.OBJECT_STORE) from exc
        request.app.state.object_store = configured
    return configured


def get_object_store(
    request: Request, settings: Annotated[Settings, Depends(get_settings)]
) -> ObjectStore:
    return object_store_from_request(request, settings)


ObjectStoreDep = Annotated[ObjectStore, Depends(get_object_store)]


def get_generation_provider(
    request: Request, settings: Annotated[Settings, Depends(get_settings)]
) -> GenerationProvider:
    configured: GenerationProvider | None = getattr(
        request.app.state, "generation_provider", None
    )
    if configured is None:
        configured = create_generation_provider(settings)
        request.app.state.generation_provider = configured
    return configured


GenerationProviderDep = Annotated[GenerationProvider, Depends(get_generation_provider)]


def get_delivery_secret_sealer(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> SecretSealer:
    """Return the API plane's encrypt-only E-1 capability."""
    configured: SecretSealer | None = getattr(
        request.app.state, "delivery_secret_sealer", None
    )
    if configured is not None:
        return configured

    if settings.email_delivery_local_key is not None:
        configured = LocalAeadCipher.from_base64(
            settings.email_delivery_local_key.get_secret_value()
        )
    elif settings.email_delivery_kms_key_id is not None:
        configured = KmsSealer(
            kms_client(settings),
            key_id=settings.email_delivery_kms_key_id,
            policy=kms_policy(settings),
            circuit=kms_circuit(settings),
        )
    else:
        raise RuntimeError(
            "EMAIL_DELIVERY_LOCAL_KEY or EMAIL_DELIVERY_KMS_KEY_ID is required "
            "before E-1 material can be issued"
        )

    request.app.state.delivery_secret_sealer = configured
    return configured


DeliverySecretSealerDep = Annotated[SecretSealer, Depends(get_delivery_secret_sealer)]


def get_ops_totp_unsealer(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> SecretUnsealer:
    """Return the operations sign-in path's decrypt-only TOTP capability."""
    configured: SecretUnsealer | None = getattr(
        request.app.state, "ops_totp_unsealer", None
    )
    if configured is not None:
        return configured
    if settings.ops_totp_local_key is not None:
        configured = LocalAeadCipher.from_base64(
            settings.ops_totp_local_key.get_secret_value()
        )
    elif settings.ops_totp_kms_key_id is not None:
        configured = KmsUnsealer(
            kms_client(settings),
            policy=kms_policy(settings),
            circuit=kms_circuit(settings),
        )
    else:
        raise RuntimeError(
            "OPS_TOTP_LOCAL_KEY or OPS_TOTP_KMS_KEY_ID is required for operations sign-in"
        )
    request.app.state.ops_totp_unsealer = configured
    return configured


OpsTotpUnsealerDep = Annotated[SecretUnsealer, Depends(get_ops_totp_unsealer)]


def get_totp_replay_store(
    valkey: Annotated[Valkey, Depends(get_valkey)],
) -> TotpReplayStore:
    return TotpReplayStore(valkey)


TotpReplayStoreDep = Annotated[TotpReplayStore, Depends(get_totp_replay_store)]


def client_address(request: Request) -> str:
    """The caller's address, for the per-source sign-in throttle.

    The **peer** address, not `X-Forwarded-For`. A forwarded header is caller-
    supplied unless something strips and rewrites it, so trusting one here would
    hand every attacker a free reset of their own counter: send a fresh
    `X-Forwarded-For` per request and the limit never trips.

    That makes this correct only behind an edge that terminates the connection and
    presents itself as the peer — which is the deployment (infra/00). Reading a
    forwarded header safely means knowing how many proxies to trust and counting
    from the right end, and getting that wrong is a silent bypass rather than a
    visible error. If the edge ever stops being the peer, this needs a trusted-hop
    count in settings, not a hopeful `request.headers.get`.

    `unknown` groups every client Starlette could not attribute into one bucket.
    That is a shared counter and it is the safe direction: unattributable traffic
    throttles as a group rather than escaping the limit entirely.
    """
    return request.client.host if request.client else "unknown"


ClientAddress = Annotated[str, Depends(client_address)]
"""The caller's peer address, resolved as a dependency like every other
request-scoped value here.

A route takes this instead of a bare `Request`, which keeps the handler's
signature a statement of what it needs and lets a test override the address
through `dependency_overrides` rather than by forging a transport."""


async def enforce_ops_sign_in_rate(
    throttle: ThrottleDep,
    settings: Annotated[Settings, Depends(get_settings)],
    source: ClientAddress,
) -> None:
    """Apply the five-per-minute source limit to ops sign-in only."""
    await operations_service.guard_ops_sign_in(throttle, settings, source=source)


def session_cookie(request: Request) -> str:
    """The raw session id from the cookie.

    Raises:
        ProblemError: `401 session-invalid` when absent. Absent, malformed and
            expired all answer identically — the client's response to every one
            of them is to route to sign-in, so distinguishing them would only
            help someone probing.
    """
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        raise ProblemError(catalog.SESSION_INVALID)
    return raw


def ops_session_cookie(request: Request) -> str:
    """Return only the operations cookie, never the customer cookie."""
    raw = request.cookies.get(OPS_SESSION_COOKIE)
    if not raw:
        raise ProblemError(catalog.OPS_SESSION_INVALID)
    return raw


async def current_ops_session(
    raw: Annotated[str, Depends(ops_session_cookie)],
    store: OpsSessionStoreDep,
) -> OpsSessionRecord:
    """Resolve an ops session and authoritatively re-check active status."""
    record = await store.resolve(raw)
    if record is None:
        raise ProblemError(catalog.OPS_SESSION_INVALID)
    try:
        ops_account_id = UUID(record.ops_account_id)
    except ValueError:
        await store.revoke(raw)
        raise ProblemError(catalog.OPS_SESSION_INVALID) from None
    async with scoped_transaction(ops_scope(ops_account_id=ops_account_id)) as db:
        try:
            await operations_service.load_ops_account(db, ops_account_id)
        except ProblemError:
            await store.revoke(raw)
            raise
    return record


OpsPrincipal = Annotated[OpsSessionRecord, Depends(current_ops_session)]


async def current_session(
    raw: Annotated[str, Depends(session_cookie)],
    store: SessionStoreDep,
) -> SessionRecord:
    """Resolve the cookie to a live session record, sliding its idle clock.

    Raises:
        ProblemError: `401 session-invalid` for unknown, expired or revoked.
    """
    record = await store.resolve(raw)
    if record is None:
        raise ProblemError(catalog.SESSION_INVALID)
    if record.gate is not None and record.gate not in GATE_PROBLEMS:
        raise ProblemError(catalog.SESSION_INVALID)
    # Only the self-account read uses the stored scope. Product queries receive
    # current membership; no stale team or gate can authorize a request.
    async with scoped_transaction(scope_of(record)) as db:
        try:
            account, _org = await service.load_principal(db, UUID(record.account_id))
        except ProblemError:
            await store.revoke(raw)
            raise
        if account.role != record.role or str(account.org_id) != record.org_id:
            # Privilege changes require a new identifier through sign-in.
            await store.revoke(raw)
            raise ProblemError(catalog.SESSION_INVALID)
        gates = await pending_gates(db, account)
        if record.gate is not None and record.gate not in gates:
            # Another device cleared this stored gate. Its privilege change must
            # not upgrade an older limited cookie in place (security/04 §3).
            await store.revoke(raw)
            raise ProblemError(catalog.SESSION_INVALID)
        return replace(
            record, team_id=str(account.team_id), gate=gates[0] if gates else None
        )


GatedPrincipal = Annotated[SessionRecord, Depends(current_session)]
"""A session that MAY be gate-limited. Only the endpoints that clear a gate —
and `GET /auth/session`, which is how the client learns which gate — take this."""


async def current_principal(record: GatedPrincipal) -> SessionRecord:
    """A session that has cleared every gate.

    Raises:
        ProblemError: `409` naming the pending gate (api/00 §3), or `401
            session-invalid` for a gate this release does not recognise.
    """
    if record.gate is not None:
        # `.get`, not `[]`. `SessionRecord.gate` is `str | None` and cannot be the
        # `Gate` literal — `Gate` lives in `modules.identity.schemas`, and
        # `platform` sits below `modules` in the layers contract, so importing it
        # there is an upward import and a build failure. The type therefore cannot
        # rule out a value this release has never heard of.
        #
        # And one can arrive. `_decode` drops unknown *keys* so that a release N+1
        # record stays readable by release N during a rolling deploy — but it
        # passes any *value* through for a key it knows. So N+1 writing a new gate
        # kind reaches here as an ordinary string, and a subscript would `KeyError`
        # into a 500 on every request from that user: exactly the outage `_decode`
        # was written to prevent, one layer up from where it was prevented.
        #
        # `session-invalid` is the honest refusal. This process cannot evaluate the
        # limitation, so it must not admit the session, and 401 routes the client to
        # sign-in — where whichever release answers will apply its own gate rules.
        raise ProblemError(GATE_PROBLEMS.get(record.gate, catalog.SESSION_INVALID))
    return record


CurrentPrincipal = Annotated[SessionRecord, Depends(current_principal)]
"""The default for every product endpoint. Refuses a gate-limited session."""


async def current_manager(record: CurrentPrincipal) -> SessionRecord:
    """A principal whose role is manager (FR-TRM-001, FR-IDA-009).

    ## Why this is a guard and not a filter

    On `/me` the database is the boundary: a rep reading `v_counted_attempt` sees
    their own rows and RLS refuses the rest. The team surface inverts that — a
    manager is *supposed* to see their whole team, so the same policies that
    protect `/me` return rows for a rep here too. `v_team_month` read by a rep is
    not denied; it answers, with a "team average" computed from that one rep's
    attempts. A wrong number, rendered confidently, on a surface they should not
    have reached at all.

    So the role check has to happen before the query, and it has to happen in one
    place that every team route takes, rather than as a predicate each of them
    remembers to add.

    ## `!=` rather than `Role(record.role)`

    The same rolling-deploy argument `current_principal` makes about gates. A
    release N+1 record can carry a role string release N has never heard of, and
    coercing it would raise `ValueError` into a 500 on every request from that
    user. Comparing instead means an unrecognised role is simply not `manager`,
    which is the closed direction.

    Raises:
        ProblemError: `404 not-found` for any other role — never `403`. A 403
            confirms the route exists and that this caller is merely not allowed,
            which is the disclosure AC-IDA-006 forbids and ADR-0036 §2 closed;
            `not_found` names this exact case in its own contract.
    """
    if record.role != Role.MANAGER:
        raise not_found()
    return record


ManagerPrincipal = Annotated[SessionRecord, Depends(current_manager)]
"""The six `/team` reads and `PUT /drills/{id}/assignment`.

**Not the drill authoring routes.** `createDrill` and the rest of the lifecycle
take `CurrentPrincipal`: the contract has authoring follow the caller's role —
"managers author team drills; reps author self-authored private drills
(FR-TRP-009)" — so gating them here would remove a rep's ability to self-author
and take FR-TRP-009's privacy story with it. The mode is decided by the service
from the role, not by refusing the request.

Composed on `CurrentPrincipal`, not on `current_session`, so the gates still run
first: a manager who has not cleared consent is answered with their gate, not
admitted because their role was right. `test_team_gate.py` pins that, because
"true by construction" is what a later refactor breaks silently."""


def scope_of(record: SessionRecord) -> ScopeContext:
    """The scope tuple every query runs under.

    Built from the session record rather than from anything the request carries,
    so no header, body field or query parameter can widen it. This is the value
    `apply_scope` writes into the transaction-local GUCs the RLS policies read.

    ## Why the role is checked here rather than upstream

    This is the single funnel: every call site — the gate-exit routes that take
    `GatedPrincipal` included — reaches the database through this function. A
    check in `current_principal` would cover the product surface and miss the
    routes that deliberately bypass it.

    Raises:
        ProblemError: `401 session-invalid` for a role this release does not
            recognise — the same answer `current_principal` gives an unrecognised
            gate, for the same reason. `_decode` passes through any *value* for a
            key it knows, so a release N+1 record naming a new role reaches N as
            an ordinary string; `Role(record.role)` would raise `ValueError` into
            a 500 on every request from that user, which is the outage `_decode`
            exists to prevent. This process cannot evaluate the claim, so it must
            not admit the session, and 401 routes the client to sign-in where
            whichever release answers applies its own rules.

            Not `404`: `current_manager` answers a *known* role that may not
            reach a route, which is a scope question. This is a session this
            process cannot read at all, and on `/me/progress` a 404 would tell a
            rep their own progress does not exist.
    """
    if record.role not in tuple(Role):
        raise ProblemError(catalog.SESSION_INVALID)
    return ScopeContext.account(
        org_id=UUID(record.org_id),
        team_id=UUID(record.team_id),
        account_id=UUID(record.account_id),
        role=Role(record.role),
    )


async def current_candidate(
    authorization: Annotated[str | None, Header()] = None,
) -> CandidateBinding:
    """Resolve an invite bearer token into its immutable candidate scope."""
    if authorization is None or not authorization.startswith("Bearer "):
        metrics.record_candidate_probe(kind="token_invalid")
        raise ProblemError(catalog.TOKEN_INVALID)
    presented = authorization[7:].strip()
    if not presented:
        metrics.record_candidate_probe(kind="token_invalid")
        raise ProblemError(catalog.TOKEN_INVALID)
    async with scoped_transaction(ScopeContext.anonymous()) as db:
        row = (
            await db.execute(
                text("select * from app_candidate_token_binding(:token_hash)"),
                {"token_hash": hash_token(presented)},
            )
        ).mappings().one_or_none()
    stored = (
        StoredCandidateToken(
            token_hash=str(row["token_hash"]),
            org_id=UUID(str(row["org_id"])),
            team_id=UUID(str(row["team_id"])),
            position_id=UUID(str(row["position_id"])),
            candidate_id=UUID(str(row["candidate_id"])),
            expires_at=row["expires_at"],
            revoked=bool(row["revoked"]),
        ) if row is not None else None
    )
    try:
        return verify_candidate_token(presented, stored)
    except TokenInvalid as exc:
        metrics.record_candidate_probe(kind=exc.outcome.value)
        if exc.outcome is TokenOutcome.EXPIRED and stored is not None:
            raise ProblemError(catalog.TOKEN_EXPIRED, meta={"expired_at": stored.expires_at.isoformat()}) from None
        raise ProblemError(catalog.TOKEN_INVALID) from None


CandidatePrincipal = Annotated[CandidateBinding, Depends(current_candidate)]
# A candidate capability resolved to `(org, team, position, candidate)`.
