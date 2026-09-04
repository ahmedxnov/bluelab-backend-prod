"""The manager gate, asserted without a database.

`ManagerPrincipal` is the only thing standing between a rep and the whole team
surface — rosters, leaderboards, other people's ratings. RLS does not stand there
too: a rep reading `v_team_month` is not denied, they are answered, with a "team
average" computed from their own attempts alone. Wrong, and quietly wrong. So the
guard is the boundary, and these pin it.

Pure on purpose. The gate's decision depends on nothing but the session record, so
proving it needs no postgres and no HTTP — and running in the unit job means a
regression is caught before the L3 suite has connected.

Two properties matter more than the happy path:

    1. an unrecognised role FAILS CLOSED, rather than raising into a 500
    2. the refusal is the generic `not-found`, carrying nothing

The second is AC-IDA-006. `not_found()`'s own docstring names this case — "a
manager's URL for a route their role does not mount" — and a `403` here would
disclose that the route exists and the rep simply is not allowed, which is the
leak that ADR-0036 §2 closed for every other resource.
"""

from __future__ import annotations

import pytest

from bluelab.api.deps import current_manager, scope_of
from bluelab.platform.db.scope import Role
from bluelab.platform.errors import catalog
from bluelab.platform.errors.denial import ProblemError
from bluelab.platform.security.sessions import SessionRecord

pytestmark = [pytest.mark.l1_unit, pytest.mark.l7_security, pytest.mark.invariant_path]


def _record(role: str, *, gate: str | None = None) -> SessionRecord:
    """A resolved session carrying `role`. The clocks are never read here."""
    return SessionRecord(
        account_id="11111111-1111-1111-1111-111111111111",
        org_id="22222222-2222-2222-2222-222222222222",
        team_id="33333333-3333-3333-3333-333333333333",
        role=role,
        created_at="2026-08-01T00:00:00Z",
        last_seen_at="2026-08-01T00:00:00Z",
        absolute_expires_at="2026-08-08T00:00:00Z",
        gate=gate,
    )


async def test_manager_passes_through_unchanged() -> None:
    """The gate returns the record it was given, not a copy or a projection.

    Callers build the scope tuple from this value, so substituting anything here
    would substitute what every downstream query runs as.
    """
    record = _record("manager")

    assert await current_manager(record) is record


async def test_rep_is_refused() -> None:
    record = _record("rep")

    with pytest.raises(ProblemError) as raised:
        await current_manager(record)

    assert raised.value.problem is catalog.NOT_FOUND
    assert raised.value.problem.status == 404


async def test_an_unrecognised_role_fails_closed() -> None:
    """A role string this release has never heard of must be refused, not crash.

    The same rolling-deploy argument `current_principal` makes about gates applies
    to roles: `_decode` passes through any *value* for a key it knows, so release
    N+1 introducing a role reaches release N as an ordinary string. Coercing it
    with `Role(record.role)` would raise `ValueError` into a 500 on every request
    from that user — an outage, and one that reads as a server fault rather than
    as the refusal it should be.

    Refusing anything that is not exactly `manager` is the closed direction: an
    unknown role gets the same answer as a rep.
    """
    record = _record("auditor")

    with pytest.raises(ProblemError) as raised:
        await current_manager(record)

    assert raised.value.problem is catalog.NOT_FOUND


async def test_the_refusal_carries_nothing_that_could_vary() -> None:
    """AC-IDA-006 as a property of the exception, not of the rendered bytes.

    A denial must be indistinguishable from a genuinely absent resource. The
    rendered body differs only in `request_id`, and that holds only while the
    problem carries no `detail` and no `meta` — which is exactly what a caller
    would be tempted to add ("role manager required") the first time they debug
    one of these.
    """
    with pytest.raises(ProblemError) as raised:
        await current_manager(_record("rep"))

    assert raised.value.detail is None
    assert raised.value.meta == {}
    assert raised.value.headers == {}


# ── scope_of: the same discipline at the other end ────────────────────────────


@pytest.mark.parametrize("role", ["manager", "rep"])
def test_a_known_role_builds_the_scope_tuple(role: str) -> None:
    scope = scope_of(_record(role))

    assert scope.role is Role(role)
    assert str(scope.account_id) == "11111111-1111-1111-1111-111111111111"


def test_an_unrecognised_role_does_not_reach_the_database() -> None:
    """`scope_of` is the single funnel, so this is where the check has to live.

    Every call site reaches the database through here — including the gate-exit
    routes on `GatedPrincipal`, which never pass through `current_principal`. A
    check placed upstream would cover the product surface and miss those.

    `Role(record.role)` used to run unguarded, so an unrecognised role raised
    `ValueError` into a `500` on every request from that user. Reachable on the
    rep surface, where no role check precedes it.
    """
    with pytest.raises(ProblemError) as raised:
        scope_of(_record("auditor"))

    assert raised.value.problem is catalog.SESSION_INVALID
    assert raised.value.problem.status == 401


def test_the_unknown_role_refusal_is_not_a_value_error() -> None:
    """Pins the failure MODE, not just the status.

    The regression this guards against is subtle: reverting to `Role(...)` still
    "fails" on an unknown role, so a test asserting only that something raised
    would keep passing while the answer went back to being a 500.
    """
    with pytest.raises(ProblemError):
        scope_of(_record("auditor"))

    with pytest.raises(ValueError):
        Role("auditor")  # the coercion that must not be reached
