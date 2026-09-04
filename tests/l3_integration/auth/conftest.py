"""Fixtures for the Auth surface (L3 — real postgres, real RLS).

These drive the actual ASGI app against the actual database, so a test that
passes here is evidence about the deployed path rather than about a mock: the
sign-in lookup really goes through `app_account_for_sign_in`, the response body
really comes back under `account_self_read`, and a cross-scope read really is
denied by a policy rather than by a Python `if`.

## Valkey is faked; postgres is not

`fakeredis` stands in for the coordination store. That substitution is safe in a
way a database one would not be: `SessionStore` is exercised in full — pipelines,
sets, TTLs, the account index — and none of the product's *invariants* live in
Valkey. Every rule this suite cares about is enforced by RLS or by
`identity.service`, and both are real here.

Faking postgres instead would delete the thing under test.

## Why the seeded addresses are `@example.com`

RFC 2606 reserves both `.test` and `example.com`. The rest of the L3 suite uses
`.test` because it inserts rows straight through SQL, where nothing validates
them. These accounts arrive through `POST /auth/session`, so the address is parsed
by `EmailStr` — and `email-validator` rejects `.test` as a special-use domain.
`example.com` is reserved for exactly this and passes.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

import fakeredis.aioredis
import pytest
import pytest_asyncio
from fastapi import APIRouter
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tests.support import required_url

from bluelab.api.deps import CurrentPrincipal
from bluelab.platform.config import Settings
from bluelab.platform.ids import new_id
from bluelab.platform.security.passwords import hash_password
from bluelab.platform.security.sessions import SessionStore

MIGRATION_URL = required_url("TEST_MIGRATION_URL")
APP_URL = required_url("TEST_DATABASE_URL")

# The engine resolves its own settings.
#
# `scoped_transaction` -> `_sessionmaker` -> `get_engine` -> `get_settings()`, and
# that last call is a module-level `lru_cache`, not a FastAPI dependency — so
# `app.dependency_overrides` cannot reach it, and overriding it there silently
# leaves the engine reading the real environment. These defaults are what make the
# engine resolvable in a bare test process; `setdefault`, so an explicitly
# exported value still wins.
os.environ.setdefault("DATABASE_URL", APP_URL)
os.environ.setdefault("VALKEY_URL", "redis://127.0.0.1:6379/0")
os.environ.setdefault("AGENT_HMAC_SECRET", "test-only-not-a-real-secret")

PASSWORD = "correct-horse-battery-staple"  # pragma: allowlist secret
"""Long enough to satisfy the policy floor, and the same for every seeded account
so a test that gets a `401` cannot be blamed on the fixture."""


def app_settings(**overrides: object) -> Settings:
    """Settings for the app under test — **`staging`, not `local`**.

    `overrides` takes env-var aliases (`AUTH_THROTTLE_IDENTIFIER_ATTEMPTS=2`), so a
    test can tighten one policy without a second construction path drifting from
    this one.

    `cookie_secure` is the one environment-conditional value in the whole config,
    and it is False on `local` because a dev server over plain http cannot set
    `Secure`. But a `__Host-` cookie *requires* `Secure`, so `session_spec`
    refuses that combination at construction — correctly, and this suite hit it.

    Running as `staging` therefore exercises the production-shaped cookie, which
    is the only shape worth asserting SEC-002 against. Testing the local variant
    would be testing the one configuration the product never ships.

    Constructed directly rather than through `get_settings()`, which is
    `lru_cache`d: whichever test imported first would otherwise fix the settings
    for the whole session.
    """
    return Settings(
        BLUELAB_ENV="staging",
        DATABASE_URL=APP_URL,
        VALKEY_URL="redis://127.0.0.1:6379/0",  # never dialled — see the client fixture
        AGENT_HMAC_SECRET="test-only-not-a-real-secret",  # pragma: allowlist secret
        **overrides,
    )


@dataclass(frozen=True, slots=True)
class World:
    org: UUID
    manager: UUID
    manager_email: str
    rep: UUID
    rep_email: str
    initial: UUID
    """An account still holding its provisioned credential — the first-sign-in
    gate."""
    initial_email: str
    deactivated: UUID
    deactivated_email: str


@pytest_asyncio.fixture(scope="session")
async def auth_engine():
    engine = create_async_engine(MIGRATION_URL)
    yield engine
    await engine.dispose()


ENCODED_PASSWORD = hash_password(PASSWORD)
"""Hashed once for the module. argon2id is memory-hard by design — roughly 50ms a
call — and re-deriving the same constant per test would spend most of this
suite's runtime proving argon2 is slow."""


@pytest_asyncio.fixture
async def world(auth_engine) -> World:
    """Four accounts covering every branch sign-in can take.

    Seeded as the migration role: the world has to exist before any principal can
    be admitted to it or refused from it.

    **Function-scoped, and that is not an oversight.** The isolation suite
    truncates every customer table in its own session-scoped setup
    (`isolation/conftest.py::_reset`). A session-scoped world here would survive
    only while `auth` sorts before `isolation`, which is an ordering nobody
    declared and pytest does not promise — and tests/README makes
    order-independence mandatory. Re-seeding per test costs one insert of five
    rows; the argon2 hash, which is the only expensive part, is module-level
    above.
    """
    ids = {name: new_id() for name in ("org", "manager", "rep", "initial", "deactivated")}
    # The RANDOM tail, not the head. These are UUIDv7s: the leading bytes are a
    # millisecond timestamp, so `str(id)[:8]` is identical for every id minted in
    # the same ~4-minute window — which collided against `uq_account_email` on the
    # second run of this suite.
    suffix = str(ids["org"]).replace("-", "")[-12:]
    emails = {
        "manager": f"mgr-{suffix}@example.com",
        "rep": f"rep-{suffix}@example.com",
        "initial": f"new-{suffix}@example.com",
        "deactivated": f"gone-{suffix}@example.com",
    }
    maker = async_sessionmaker(auth_engine, expire_on_commit=False)
    async with maker() as s, s.begin():
        await s.execute(
            text("insert into org (id, name, timezone) values (:id, 'Auth Org', 'Africa/Cairo')"),
            {"id": ids["org"]},
        )

        async def account(key: str, *, team: UUID, role: str, credential: str, status: str) -> None:
            await s.execute(
                text(
                    "insert into account (id, org_id, team_id, email, display_name, role,"
                    " password_hash, credential_state, status)"
                    " values (:id, :org, :team, :email, :name, :role, :hash, :cred, :status)"
                ),
                {
                    "id": ids[key], "org": ids["org"], "team": team, "email": emails[key],
                    "name": key.title(), "role": role, "hash": ENCODED_PASSWORD,
                    "cred": credential, "status": status,
                },
            )

        # The manager is its own team (FR-IDA-009), so it must exist first.
        await account("manager", team=ids["manager"], role="manager", credential="set", status="active")
        await account("rep", team=ids["manager"], role="rep", credential="set", status="active")
        await account("initial", team=ids["manager"], role="rep", credential="initial", status="active")
        await account(
            "deactivated", team=ids["manager"], role="rep", credential="set", status="deactivated"
        )

    return World(
        org=ids["org"],
        manager=ids["manager"], manager_email=emails["manager"],
        rep=ids["rep"], rep_email=emails["rep"],
        initial=ids["initial"], initial_email=emails["initial"],
        deactivated=ids["deactivated"], deactivated_email=emails["deactivated"],
    )


@pytest_asyncio.fixture
async def client(world) -> AsyncIterator[AsyncClient]:
    """The real app, over ASGI, with a fake Valkey.

    `create_app` rather than the module-level `app`: each test gets its own
    instance and its own empty session store, so a session opened by one test
    cannot authenticate another. Order-independence is mandatory (tests/README),
    and a shared session store is the easiest way to lose it.
    """
    from bluelab.entrypoints.api import create_app
    from bluelab.platform.config import get_settings

    settings = app_settings()
    app = create_app(settings)

    # `create_app(settings)` only configures the app object. Routes resolve their
    # own settings through `Depends(get_settings)`, which reads the process
    # environment and is `lru_cache`d — so without this override the handlers
    # would use a completely different Settings from the one the app was built
    # with, and in a bare test process that construction fails outright.
    app.dependency_overrides[get_settings] = lambda: settings

    app.state.valkey = fakeredis.aioredis.FakeRedis(decode_responses=True)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://api.test") as http:
        # The lifespan is deliberately not run: it would build a real Valkey
        # client and overwrite the fake above. The engine is lazily built by
        # `get_engine()` on first use and disposed by the session-scoped fixture.
        yield http

    await app.state.valkey.aclose()


PROBE_PATH = "/api/v1/_probe"

_probe_router = APIRouter()


@_probe_router.get(PROBE_PATH)
async def _probe(record: CurrentPrincipal) -> dict[str, str]:
    """A stand-in for every product endpoint, which will all take `CurrentPrincipal`.

    Declared at module scope, and it has to be. This file runs under
    `from __future__ import annotations`, so `record: CurrentPrincipal` is stored as
    the *string* `"CurrentPrincipal"` and FastAPI resolves it with `get_type_hints`
    against the function's module globals. Defined inside the fixture with a
    function-local import, the name is not in those globals: resolution fails,
    FastAPI falls back to treating `record` as a query parameter, and every request
    answers `422 Field required` instead of ever reaching the dependency.
    """
    return {"account_id": record.account_id}


@dataclass(frozen=True, slots=True)
class Guarded:
    """A client whose app carries one route behind `CurrentPrincipal`."""

    http: AsyncClient
    store: SessionStore
    """A store over the same fake Valkey, for minting records the sign-in path
    cannot produce — an unrecognised gate, in particular."""

    PROBE = PROBE_PATH


@pytest_asyncio.fixture
async def guarded(world) -> AsyncIterator[Guarded]:
    """The app plus a throwaway route that demands a fully-admitted principal.

    `CurrentPrincipal` is the default every product endpoint will take, and its
    refusal is the deny-by-default guarantee `api.deps` is built around — a route
    that forgets to think about gates still fails closed. Until this fixture there
    was nothing to point it at: the three Auth routes all take `GatedPrincipal` or
    no principal at all, so the refusing branch had no caller in the whole suite.

    The probe is mounted here rather than shipped, because inventing a product
    endpoint in order to test a dependency would tie this suite to whichever
    endpoint happened to get invented first.
    """
    from bluelab.entrypoints.api import create_app
    from bluelab.platform.config import get_settings

    settings = app_settings()
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.valkey = fakeredis.aioredis.FakeRedis(decode_responses=True)
    app.include_router(_probe_router)

    store = SessionStore(
        app.state.valkey,
        idle_seconds=settings.session_idle_seconds,
        absolute_seconds=settings.session_absolute_seconds,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://api.test") as http:
        yield Guarded(http=http, store=store)

    await app.state.valkey.aclose()


@pytest_asyncio.fixture
async def throttled(world) -> AsyncIterator[AsyncClient]:
    """A client whose sign-in limits are small enough to reach in a test.

    Two attempts per identifier, three per source. The production defaults (6 and
    60) are policy, not behaviour — reaching them here would spend a minute of
    argon2 per test to prove arithmetic. What these assert is that the limit
    exists, trips, refuses before the hash runs, and clears on success.
    """
    from bluelab.entrypoints.api import create_app
    from bluelab.platform.config import get_settings

    settings = app_settings(
        AUTH_THROTTLE_IDENTIFIER_ATTEMPTS=2,
        AUTH_THROTTLE_SOURCE_ATTEMPTS=3,
    )
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.valkey = fakeredis.aioredis.FakeRedis(decode_responses=True)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://api.test") as http:
        yield http

    await app.state.valkey.aclose()


@pytest_asyncio.fixture
async def legal_version(auth_engine):
    """Publish a legal document version, which opens a gate for everyone at once.

    Written as the migration role: `legal_document_version` is P0 reference data
    owned by migrations, and the product has no write path to it.

    **Torn down after every test.** These rows are platform-wide, not scoped to
    the test's own org — `current_version` picks the newest effective row of a
    kind regardless of who is asking. A leftover would silently decide the gate
    for every later test, including ones that expect none.
    """
    published: set[str] = set()
    maker = async_sessionmaker(auth_engine, expire_on_commit=False)

    async def _publish(kind: str, version: str, *, effective_offset: int = 0) -> None:
        published.add(kind)
        async with maker() as s, s.begin():
            await s.execute(
                text(
                    "insert into legal_document_version (id, kind, version, effective_at)"
                    " values (:id, :kind, :version, now() + make_interval(mins => :off))"
                ),
                {"id": new_id(), "kind": kind, "version": version, "off": effective_offset},
            )

    yield _publish

    if published:
        async with maker() as s, s.begin():
            await s.execute(
                text("delete from legal_document_version where kind = any(:kinds)"),
                {"kinds": sorted(published)},
            )


@pytest_asyncio.fixture
async def record_consent(auth_engine):
    """Record an acceptance the way the system would.

    Also written as the migration role. `consent_record` is `system_write_only`,
    so there is deliberately no account-facing write path — the evidence trail is
    written by the platform, never by the subject of it (CMP-002).
    """

    async def _record(account_id: UUID, version: str) -> None:
        maker = async_sessionmaker(auth_engine, expire_on_commit=False)
        async with maker() as s, s.begin():
            org = (
                await s.execute(
                    text("select org_id from account where id = :id"), {"id": account_id}
                )
            ).scalar_one()
            await s.execute(
                text(
                    "insert into consent_record (id, org_id, account_id, notice_version)"
                    " values (:id, :org, :account, :version)"
                ),
                {"id": new_id(), "org": org, "account": account_id, "version": version},
            )

    return _record


@pytest.fixture
def credentials(world):
    def _for(email: str, password: str = PASSWORD) -> dict[str, str]:
        return {"email": email, "password": password}

    return _for
