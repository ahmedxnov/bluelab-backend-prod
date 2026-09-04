"""`/api/v1` — the customer surface, composed from the module routers along the tag
groups of `api/openapi.yaml`:

    Auth, Legal          identity
    Me, Library, Team    training
    Drills               drills
    Knowledge            knowledge
    Calls                calls
    Attempts             review
    Hiring               hiring
    Assessment           assessment

**All product data access goes through this surface.** No client reaches storage,
PostgREST, or any store directly — the concealment rule and scope filtering are
enforced server-side and would be bypassed by any second path (api/00 §1).

The prefix lives here and nowhere else. Each router declares bare paths
(`/auth/session`, not `/api/v1/auth/session`) so that the URL major version is a
single composition decision — which is what makes minting `/api/v2` with a bounded
overlap window a mount change rather than an edit to every route in the tree
(ADR-0039).
"""

from __future__ import annotations

from fastapi import APIRouter

from bluelab.api import auth, team, training

PREFIX = "/api/v1"

router = APIRouter(prefix=PREFIX)

# Auth carries no `sessionCookie` requirement of its own: sign-in must be
# reachable unauthenticated, and the two authenticated routes resolve the cookie
# through their own dependencies. The contract's global `security` is the
# *default*, and `POST /auth/session` overrides it to `[]` (api/openapi.yaml).
router.include_router(auth.router)

# The rep surface. Every route takes `CurrentPrincipal`, so the global
# `sessionCookie` requirement applies and a gate-limited session is refused with
# the `409` naming its gate before any handler runs.
router.include_router(training.router)

# The manager surface. Every route takes `ManagerPrincipal`, which composes on
# `CurrentPrincipal` — so the gates still run first, and a role that is not
# `manager` is refused with the generic `404` rather than a `403` that would
# disclose the route exists (ADR-0036 §2).
router.include_router(team.router)
