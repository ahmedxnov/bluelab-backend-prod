"""Application-plane entrypoint — the app factory uvicorn serves.

Composition only. Nothing here decides behaviour: it wires the routers, the
exception handlers, the security headers, and the clients that outlive a request.
A rule that lived in this file would only hold when the process was started this
way — which is why the gate checks live in `api.deps` and the authentication rules
in `modules.identity.service`.

## A factory, and no module-level `app`

`create_app()` builds a fresh instance per call, so a test can stand one up with
its own settings without inheriting whatever a previous import left behind.

There is deliberately no `app = create_app()` at module scope. That line would
resolve `Settings` at *import* time, which makes the module unimportable unless
every environment variable the whole application needs is already set — including
in a test that only wanted the factory. uvicorn is pointed at the factory instead
(`--factory`), which costs nothing and keeps importing this module free of side
effects.

## The lifespan holds exactly two things

The Valkey client and the database engine. Both are pools: built once, shared for
the process, disposed on shutdown. Building either per request would open a
connection per request, which is how a pool becomes the outage it was meant to
absorb.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI
from valkey.asyncio import Valkey

from bluelab.api import health, v1
from bluelab.platform.config import Environment, Settings, get_settings
from bluelab.platform.db.engine import dispose_engine
from bluelab.platform.errors import handlers
from bluelab.platform.http.rate_limit import RateLimitMiddleware
from bluelab.platform.http.security_headers import SecurityHeadersMiddleware


def _lifespan(
    settings: Settings,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.valkey = Valkey.from_url(
            settings.valkey_url.get_secret_value(), decode_responses=True
        )
        try:
            yield
        finally:
            # Both are released even if the first raises. A leaked pool outlives
            # the process it belonged to when the runner reloads.
            try:
                await app.state.valkey.aclose()
            finally:
                await dispose_engine()

    return lifespan


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application-plane app.

    Args:
        settings: Overridden by tests. Defaults to the process settings.

    Returns:
        A configured `FastAPI` instance with no process-wide side effects.
    """
    resolved = settings or get_settings()

    app = FastAPI(
        title="BlueLab v1 API",
        version="1.0.0",
        lifespan=_lifespan(resolved),
        # FastAPI serves `/docs`, `/redoc` and `/docs/oauth2-redirect` by default,
        # unauthenticated. They are off here in every environment: the authored
        # contract is `api/openapi.yaml` and that is what anyone entitled to read
        # it should read. Interactive explorers on a production origin publish the
        # shape of every endpoint, every schema and every problem type to anyone
        # who asks (A05:2025 — disable defaults, minimise features).
        docs_url=None,
        redoc_url=None,
        # The generated schema is an OUTPUT, never the contract (ADR-0035).
        #
        # It used to be served in every environment, on the grounds that
        # `tools/check_conformance_diff.py` reads it. It does not: that tool
        # imports the app and calls `app.openapi()` on the object. Nothing has
        # ever fetched this URL, so serving it in prod bought the gate nothing and
        # published the surface for free.
        #
        # Kept off prod, left on elsewhere, where a live schema is genuinely useful
        # for local work and for pointing a mock at.
        openapi_url=None if resolved.environment is Environment.PROD else "/openapi.json",
    )

    # ORDER MATTERS, and it is the reverse of how it reads.
    #
    # `add_middleware` PREPENDS, so the last one added is the OUTERMOST and runs
    # first. The rate limiter short-circuits — it returns a `429` without calling
    # the app beneath it — so anything that must decorate that response has to be
    # outside it. Security headers therefore go on LAST.
    #
    # Written the other way round the limiter was outermost, and a `429` came back
    # with no `X-Frame-Options`, no `Cache-Control: no-store`, none of it. Verified
    # both ways: this is not a theoretical ordering point.
    app.add_middleware(
        RateLimitMiddleware,
        limit=resolved.request_limit_per_window,
        window_seconds=resolved.request_limit_window_seconds,
    )
    app.add_middleware(SecurityHeadersMiddleware, enable_hsts=resolved.cookie_secure)

    # Registered before the routers. Ordering does not change what catches, but it
    # makes plain that no route renders its own errors: every error on this
    # surface is RFC 9457 from one place (api/00 §5).
    handlers.register(app)

    app.include_router(health.router)
    app.include_router(v1.router)
    return app


def main() -> None:
    """The `bluelab-api` console script."""
    import uvicorn

    uvicorn.run(
        "bluelab.entrypoints.api:create_app",
        factory=True,
        # Bound inside the container and published by the edge — the process never
        # listens on a routable interface. bandit flags this as B104; the `noqa`
        # that used to carry the reason was for a rule this repo does not enable,
        # so ruff removed it and the justification with it. Kept as prose.
        host="0.0.0.0",
        port=8000,
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
