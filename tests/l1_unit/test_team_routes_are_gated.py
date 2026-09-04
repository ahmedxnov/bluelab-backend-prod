"""No contracted manager operation may be reachable without the manager gate.

## Why this exists before the routes it guards

`ManagerPrincipal` is opt-in, and that is unremarkable — every route-level
dependency in every framework is. What is not unremarkable is the *consequence of
forgetting it here*.

On `/me`, a forgotten predicate is caught by the database: RLS returns the rep
their own rows and nothing else, so the mistake shows up as missing data rather
than as somebody else's. The team surface has no such net. A manager is supposed
to see their whole team, so the same policies answer a rep too — `v_team_month`
read by a rep is not denied, it returns a "team average" computed from that one
rep's attempts. The mistake renders as a plausible dashboard, and the only thing
standing between a rep and it is whether the route's author typed
`ManagerPrincipal` instead of `CurrentPrincipal`.

Written now, while the answer is "zero team routes", so that the six that follow
inherit it rather than have it retrofitted across them.

## Runtime inspection, not a source scan

`test_scope_is_the_only_authority.py` is static because its claim is *no other
path exists*, which a runtime test cannot show. This claim is the opposite — that
one specific path is always taken — so it walks the resolved dependency tree
FastAPI actually built. That also catches gating applied at the router level via
`APIRouter(dependencies=[...])`, which a signature scan would report as missing.

The cost is a dependency on FastAPI internals, and
`test_the_inspection_machinery_works` is the control that keeps that cost honest:
it pins the traversal against a route known to be gated, so an internals change
fails loudly there instead of quietly turning the guard below into a no-op.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.routing import APIRoute

from bluelab.api import v1
from bluelab.api.deps import current_manager, current_principal

pytestmark = [pytest.mark.l1_unit, pytest.mark.l7_security]

CONTRACT = Path(__file__).resolve().parents[4] / "api" / "openapi.yaml"
"""`api/openapi.yaml` is authoritative (ADR-0035), so the set of manager-only
operations is read from it rather than restated here. A path prefix would not do:
`PUT /drills/{drill_id}/assignment` is manager-only and does not live under
`/team`, and the contract already says so by tagging it `Team`."""

HTTP_METHODS = {"get", "put", "post", "delete", "patch"}


def _api_routes() -> list[APIRoute]:
    """Every `APIRoute` under the v1 router, flattened.

    Recent FastAPI keeps an included router as a lazy node rather than splicing
    its routes into the parent, so this descends through either shape.
    """

    def walk(routes: list[Any]) -> list[APIRoute]:
        found: list[APIRoute] = []
        for route in routes:
            if isinstance(route, APIRoute):
                found.append(route)
                continue
            nested = getattr(route, "routes", None) or getattr(
                getattr(route, "original_router", None), "routes", None
            )
            if nested:
                found.extend(walk(nested))
        return found

    return walk(list(v1.router.routes))


def _dependency_calls(dependant: Any) -> set[Any]:
    """Every callable in a route's resolved dependency tree, at any depth."""
    found = {dependant.call}
    for sub in dependant.dependencies:
        found |= _dependency_calls(sub)
    return found


def _full_path(route: APIRoute) -> str:
    """The path as the contract writes it — the `/api/v1` prefix is applied at
    mount, so `route.path` alone is the bare one."""
    return f"{v1.PREFIX}{route.path}"


def _manager_operations() -> set[tuple[str, str]]:
    spec = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    operations: set[tuple[str, str]] = set()
    for path, item in (spec.get("paths") or {}).items():
        for method, operation in item.items():
            if method.lower() not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            if "Team" in (operation.get("tags") or []):
                operations.add((method.upper(), path))
    return operations


def test_the_contract_still_names_manager_only_operations() -> None:
    """Guards the guard against a silent rename.

    Every assertion below is scoped by the `Team` tag. If that tag were renamed or
    dropped, the set would empty, the guard would iterate nothing, and it would go
    green having checked nothing at all.
    """
    operations = _manager_operations()

    assert operations, "no operation is tagged Team — the guard below checks nothing"
    assert ("GET", "/api/v1/team/dashboard") in operations
    assert ("PUT", "/api/v1/drills/{drill_id}/assignment") in operations, (
        "assignment is manager-only and outside /team — it must stay tagged Team, "
        "or a path-prefix assumption will quietly stop covering it"
    )


def test_the_inspection_machinery_works() -> None:
    """The control. Without it, the guard below passes vacuously today.

    Zero team routes are served, so the real assertion iterates an empty set. That
    is the correct answer right now — but it is also indistinguishable from a
    traversal that silently stopped finding routes. This pins both halves against
    a route known to be gated, so a FastAPI internals change breaks here loudly
    rather than there silently.
    """
    routes = _api_routes()

    assert routes, "route traversal found nothing — the guard below cannot work"
    progress = next(r for r in routes if r.path == "/me/progress")
    assert current_principal in _dependency_calls(progress.dependant)


def test_every_served_manager_operation_requires_the_manager_gate() -> None:
    """The guard itself (FR-TRM-001, FR-IDA-009).

    Iterates whatever is served today and holds each contracted manager operation
    to `current_manager` appearing somewhere in its resolved dependency tree —
    whether the route annotated `ManagerPrincipal` directly or inherited it from
    its router.
    """
    manager_operations = _manager_operations()

    ungated = [
        f"{min(route.methods)} {_full_path(route)}"
        for route in _api_routes()
        if any(
            (method, _full_path(route)) in manager_operations for method in route.methods
        )
        and current_manager not in _dependency_calls(route.dependant)
    ]

    assert not ungated, (
        "manager-only operations served without ManagerPrincipal: "
        f"{ungated}. RLS will not catch this — a rep reaching one of these is "
        "answered, not denied (see current_manager's docstring)."
    )
