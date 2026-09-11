"""The process liveness route must not depend on the coordination store."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from bluelab.entrypoints import api
from bluelab.platform.config import Settings
from bluelab.platform.http.rate_limit import RateLimitMiddleware
from bluelab.platform.security.throttle import Throttle


@pytest.mark.asyncio
async def test_healthz_bypasses_rate_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    downstream_called = False

    async def downstream(_scope: Any, _receive: Any, _send: Any) -> None:
        nonlocal downstream_called
        downstream_called = True

    async def unexpected_hit(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("healthz attempted to access Valkey")

    monkeypatch.setattr(Throttle, "hit", unexpected_hit)
    middleware = RateLimitMiddleware(downstream, limit=1, window_seconds=60)
    scope = {
        "type": "http",
        "path": "/healthz",
        "app": SimpleNamespace(state=SimpleNamespace(valkey=object())),
    }

    async def receive() -> dict[str, str]:
        return {"type": "http.request"}

    async def send(_message: Any) -> None:
        return None

    await middleware(scope, receive, send)  # type: ignore[arg-type]

    assert downstream_called


@pytest.mark.verifies("ADR-0024")
async def test_coordination_client_uses_the_shared_dependency_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeValkey:
        async def aclose(self) -> None:
            return None

    def from_url(url: str, **options: object) -> FakeValkey:
        captured.update(url=url, **options)
        return FakeValkey()

    async def dispose() -> None:
        return None

    monkeypatch.setattr(api.Valkey, "from_url", from_url)
    monkeypatch.setattr(api, "dispose_engine", dispose)
    settings = Settings(  # type: ignore[call-arg]
        DATABASE_URL="postgresql+asyncpg://app:test@db/bluelab",  # pragma: allowlist secret
        VALKEY_URL="redis://coordination:6379/0",
        AGENT_HMAC_SECRET="unit-test-only",  # pragma: allowlist secret
        DEPENDENCY_TIMEOUT_SECONDS=2.5,
    )
    app = api.create_app(settings)

    async with app.router.lifespan_context(app):
        assert app.state.valkey is not None

    assert captured == {
        "url": "redis://coordination:6379/0",
        "decode_responses": True,
        "socket_connect_timeout": 2.5,
        "socket_timeout": 2.5,
    }
