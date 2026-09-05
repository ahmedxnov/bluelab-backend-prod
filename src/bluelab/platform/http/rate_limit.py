"""The surface-wide request limit (api/05, SEC-004).

Every operation in `api/openapi.yaml` declares `429`. Wiring that per endpoint
means every future route has to remember, and the evidence that they will not is
already in the repository: sign-in shipped with no limit at all, and the two gate
exits followed it. So this sits in front of the whole surface and a new route
inherits it by existing. The process-only `/healthz` route is the sole exception:
liveness must not wait for the coordination store it helps diagnose.

## What this is NOT

It does not replace the auth throttles in `identity.service`. Those ration
argon2id — 19 MiB per verification, spent before the caller is known — and they
key on the submitted email and the source address, neither of which exists here.
This rations request VOLUME per authenticated account. Different resource,
different key, both needed.

## Keyed on the session cookie, not the address

The account id is the thing worth limiting, but resolving it means reading Valkey
and decoding the session — work this middleware runs before the router and would
then repeat in `api.deps`. Hashing the raw cookie gives the same partitioning for
free: one cookie is one session is one account, and a caller cannot get a second
bucket without authenticating again.

Unauthenticated requests share a single bucket keyed on the peer address. That is
deliberately coarse — sign-in has its own, much tighter, throttle, and everything
else on the surface answers `401` before doing work.

## Fail-open, and why this one is the exception

A Valkey failure here lets the request through. Everywhere else in this codebase
a store failure fails closed, and that is right when the store is the source of
truth for an access decision. This is not: it is a volume cap. Failing closed
would turn a coordination-store blip into a total outage of a surface that would
otherwise have served fine — trading a bounded abuse risk for an unbounded
availability one. The session lookup immediately after still fails closed, so an
unauthenticated caller gains nothing from the gap.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

from starlette.types import ASGIApp, Receive, Scope, Send

from bluelab.platform.errors import catalog
from bluelab.platform.errors.handlers import render_problem
from bluelab.platform.security.cookies import SESSION_COOKIE
from bluelab.platform.security.throttle import Limit, Throttle
from bluelab.platform.telemetry.correlation import current_request_id

_BUCKET: Final = "surface"
_HEALTH_PATH: Final = "/healthz"


def _principal_key(scope: Scope) -> str:
    """One bucket per session, falling back to the peer address.

    The cookie is read straight off the raw headers rather than resolved, because
    resolving it is exactly the work this middleware exists to bound.
    """
    # Typed explicitly: `scope` is a plain dict to mypy, so an untyped `.get`
    # makes every value below `Any` and the return type stops meaning anything.
    headers: Iterable[tuple[bytes, bytes]] = scope.get("headers", ())
    for name, value in headers:
        if name == b"cookie":
            for part in value.decode("latin-1").split(";"):
                key, _, val = part.strip().partition("=")
                if key == SESSION_COOKIE and val:
                    return val
    client: tuple[str, int] | None = scope.get("client")
    return f"anon:{client[0]}" if client else "anon:unknown"


class RateLimitMiddleware:
    """Bound request volume across the whole API surface.

    Args:
        app: The ASGI app.
        limit: Requests allowed per window, per session.
        window_seconds: The window.
    """

    def __init__(self, app: ASGIApp, *, limit: int, window_seconds: int) -> None:
        self.app = app
        self._limit = Limit(limit, window_seconds)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if scope.get("path") == _HEALTH_PATH:
            await self.app(scope, receive, send)
            return

        valkey = getattr(scope["app"].state, "valkey", None)
        if valkey is None:
            # No coordination store on this instance — a test app that never ran
            # its lifespan, for one. Nothing to count against; do not invent a
            # limiter that would silently pass everything anyway.
            await self.app(scope, receive, send)
            return

        try:
            retry = await Throttle(valkey).hit(_BUCKET, _principal_key(scope), self._limit)
        except Exception:  # noqa: BLE001 — see the module docstring on fail-open
            retry = None

        if retry is not None:
            response = render_problem(
                catalog.RATE_LIMITED,
                request_id=current_request_id(),
                headers={"Retry-After": str(retry)},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)

