"""The manager gate, through the real app.

`tests/l1_unit/test_role_gate.py` proves the decision in isolation. This proves
the *wiring*: that `ManagerPrincipal` composes with the session gates rather than
replacing them, and that the refusal renders as RFC 9457 carrying the generic
type and nothing else.

Every test here is on an invariant path — the role boundary and the denial
semantics — which is why the module carries `l7_security` and `invariant_path`.
The month-anchor tests that used to live here do not qualify and now sit in
`test_month_resolution.py`; leaving them under these marks overstated what is
inside the mutation-tested band.

## Why there is a probe route

The team surface has no routes yet — this is the dependency every one of them
will take, landing before the first so none ships an unproven guard. A dependency
with no mount point cannot be exercised end-to-end, so this module mounts one on
the app fixture. The route is real, the middleware stack is real, the session
store is real; only the handler is a stand-in, and it echoes the principal so a
test can see *which* session got through rather than merely that one did.

Mounted per-test through the `app` fixture, so it reaches no other suite and never
appears on the surface `check_conformance_diff.py` enumerates.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import APIRouter
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from bluelab.api.deps import ManagerPrincipal
from bluelab.platform.ids import new_id

pytestmark = [
    pytest.mark.l3_integration,
    pytest.mark.l7_security,
    pytest.mark.invariant_path,
]

PROBE = "/api/v1/__probe/manager"

probe_router = APIRouter()


@probe_router.get(PROBE)
async def _manager_only(record: ManagerPrincipal) -> dict[str, str]:
    """Echoes the principal that cleared the gate.

    Returning the account id rather than a constant means a test can tell "the
    gate admitted the manager" from "the gate admitted somebody".
    """
    return {"account_id": record.account_id, "role": record.role}


@pytest.fixture(autouse=True)
def mount_probe(app) -> None:
    """Before any request is made, so the route table is settled at first call."""
    app.include_router(probe_router)


@pytest_asyncio.fixture
async def privacy_notice(training_engine) -> AsyncIterator[None]:
    """Publish a privacy notice, which opens a consent gate for every account.

    Written as the migration role: `legal_document_version` is P0 reference data
    owned by migrations, and the product has no write path to it.

    **Torn down unconditionally.** These rows are platform-wide rather than scoped
    to this test's org — `current_version` picks the newest effective row of a
    kind regardless of who asks — so a leftover would silently open a gate for
    every later test, including the ones directly above that expect none.
    """
    maker = async_sessionmaker(training_engine, expire_on_commit=False)
    async with maker() as session, session.begin():
        await session.execute(
            text(
                "insert into legal_document_version (id, kind, version, effective_at)"
                " values (:id, 'privacy_notice', '2026.1', now())"
            ),
            {"id": new_id()},
        )

    yield

    async with maker() as session, session.begin():
        await session.execute(
            text("delete from legal_document_version where kind = 'privacy_notice'")
        )


async def test_a_manager_reaches_a_manager_route(client, sign_in, world):
    await sign_in(world.manager_email)

    response = await client.get(PROBE)

    assert response.status_code == 200, response.text
    assert response.json() == {"account_id": str(world.manager), "role": "manager"}


async def test_a_rep_gets_not_found_not_forbidden(client, sign_in, world):
    """AC-IDA-006 on the role boundary.

    403 would be honest HTTP and a disclosure: it confirms the route exists and
    that this rep simply is not allowed. ADR-0036 §2 closed that everywhere else,
    and `not_found()` names this exact case in its own contract.
    """
    await sign_in(world.rep_email)

    response = await client.get(PROBE)

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["type"] == "/problems/not-found"


async def test_the_rep_refusal_matches_a_genuinely_absent_route(client, sign_in, world):
    """Body equality apart from `request_id`, which is per-request by design.

    This is the assertion that would catch a well-meaning `detail="manager role
    required"` being added to the refusal — which would be a disclosure, and would
    read as a helpful error message right up until someone used it to enumerate
    which routes exist.
    """
    await sign_in(world.rep_email)

    denied = (await client.get(PROBE)).json()
    absent = (await client.get("/api/v1/__probe/manager/nope")).json()

    assert denied.pop("request_id", None) is not None
    absent.pop("request_id", None)
    assert denied == absent


async def test_an_anonymous_caller_gets_401_not_404(client):
    """The role gate must not shadow the session check.

    `current_manager` depends on `CurrentPrincipal`, so a missing session is
    answered as a missing session. Were the role read first it would have to read
    it from somewhere, and there is nowhere but the session.
    """
    response = await client.get(PROBE)

    assert response.status_code == 401
    assert response.json()["type"] == "/problems/session-invalid"


async def test_the_denial_is_logged_for_ops(client, sign_in, world, caplog):
    """ADR-0036 pays for the 404 policy with "ops-side logs retain the real cause".

    Until this line existed they did not: `handle_problem` rendered and returned,
    so a principal walking the surface to see what exists left no trace anywhere.
    The response is what must be indistinguishable — the log is a different
    surface, and the caller never observes it.

    `not-found` is the only problem logged. A gate refusal or a `422` is the
    product working and the client is told exactly what happened; logging those
    would bury the one line carrying security signal.
    """
    await sign_in(world.rep_email)

    with caplog.at_level(logging.WARNING, logger="bluelab.errors"):
        response = await client.get(PROBE)

    assert response.status_code == 404
    entry = next(r for r in caplog.records if r.message == "not_found")
    assert entry.request_id == response.json()["request_id"], (
        "the log line must carry the same support code the caller was shown, or it "
        "cannot be reached from a support ticket"
    )
    assert entry.path == PROBE
    assert entry.method == "GET"


async def test_logging_the_denial_did_not_change_the_response(client, sign_in, world, caplog):
    """AC-IDA-006 survives the observability change.

    The risk in logging a denial is that the next person reaches for the response
    body to carry what they wanted to see. This re-asserts byte-equality with a
    genuine absence while the logging is active.
    """
    await sign_in(world.rep_email)

    with caplog.at_level(logging.WARNING, logger="bluelab.errors"):
        denied = (await client.get(PROBE)).json()
        absent = (await client.get("/api/v1/__probe/manager/nope")).json()

    denied.pop("request_id")
    absent.pop("request_id")
    assert denied == absent


async def test_a_gate_limited_manager_is_answered_with_their_gate(
    client, sign_in, world, privacy_notice
):
    """The right role does not buy past a gate.

    `current_manager` depends on `CurrentPrincipal`, so consent is evaluated
    before the role is looked at — which is true *by construction* and therefore
    exactly the kind of property a later refactor breaks in silence. Rebasing this
    dependency on `current_session` to "avoid checking twice" would leave every
    other test in this module green while admitting managers who have not
    consented.

    The gate answers `409`, not the role's `404`: a gate is a disclosed
    authorization state (ADR-0036 §2) because the client has to route the user to
    the screen that clears it.
    """
    await sign_in(world.manager_email)

    response = await client.get(PROBE)

    assert response.status_code == 409, response.text
    assert response.json()["type"] == "/problems/consent-required"
