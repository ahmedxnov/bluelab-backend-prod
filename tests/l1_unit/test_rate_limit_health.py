"""The process liveness route must not depend on the coordination store."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

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
