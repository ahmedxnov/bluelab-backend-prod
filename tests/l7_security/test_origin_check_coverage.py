"""Route-wide CSRF-origin coverage for every cookie surface mutation."""

from __future__ import annotations

import re

import fakeredis.aioredis
import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from tests.l3_integration.auth.conftest import app_settings

from bluelab.entrypoints.api import create_app
from bluelab.platform.config import get_settings

pytestmark = [pytest.mark.l7_security, pytest.mark.invariant_path]

UNSAFE = frozenset({"POST", "PUT", "PATCH", "DELETE"})
COOKIE_PREFIXES = ("/api/v1", "/ops/v1")
TEST_ID = "01936d54-7ad5-7000-8000-000000000001"


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", TEST_ID, path)


def _api_routes(routes, prefix: str = ""):
    """Flatten FastAPI's lazy included-router tree with effective prefixes."""
    for route in routes:
        if isinstance(route, APIRoute):
            yield prefix + route.path, route.methods
            continue
        original = getattr(route, "original_router", None)
        if original is not None:
            context = getattr(route, "include_context", None)
            nested_prefix = getattr(context, "prefix", "")
            yield from _api_routes(original.routes, prefix + nested_prefix)


@pytest.mark.verifies("SEC-006")
async def test_every_cookie_surface_mutation_rejects_cross_site_initiation():
    settings = app_settings()
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    operations = [
        (method, path)
        for path, methods in _api_routes(app.routes)
        if path.startswith(COOKIE_PREFIXES)
        for method in sorted(methods & UNSAFE)
    ]
    assert operations, "the coverage check found no cookie-surface mutations"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://api.test") as client:
        for method, path in operations:
            response = await client.request(
                method,
                _concrete(path),
                headers={
                    "cookie": "__Host-bluelab_session=ambient-authority",
                    "sec-fetch-site": "cross-site",
                },
            )
            assert response.status_code == 400, (method, path, response.text)
            assert response.json()["type"] == "/problems/malformed-request"


@pytest.mark.verifies("SEC-006")
async def test_same_origin_is_admitted_to_route_handling_and_bearer_is_exempt():
    settings = app_settings()
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.valkey = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="https://api.test") as client:
            same_origin = await client.delete(
                "/api/v1/auth/session",
                headers={
                    "cookie": "__Host-bluelab_session=missing",
                    "origin": "https://api.test",
                },
            )
            bearer = await client.post(
                "/api/v1/assessment/preflight",
                headers={
                    "authorization": "Bearer capability",
                    "sec-fetch-site": "cross-site",
                },
                json={},
            )
    finally:
        await app.state.valkey.aclose()

    assert same_origin.status_code == 204
    assert bearer.status_code != 400 or bearer.json().get("detail") != (
        "Cross-site initiation is not accepted for this operation."
    )
