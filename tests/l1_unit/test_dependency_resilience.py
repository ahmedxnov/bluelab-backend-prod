"""Shared timeout, retry, and circuit-breaker behavior for external calls."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from bluelab.api import deps
from bluelab.platform.config import Settings
from bluelab.platform.resilience import (
    CircuitBreaker,
    CircuitOpen,
    DependencyName,
    DependencyPolicy,
    DependencyUnavailable,
    call_dependency,
)

pytestmark = pytest.mark.l1_unit


@pytest.mark.verifies("ADR-0023")
async def test_dependency_retry_is_bounded_and_can_recover():
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("provider unavailable")
        return "ok"

    result = await call_dependency(
        DependencyName.OBJECT_STORE,
        operation,
        policy=DependencyPolicy(
            timeout_seconds=1,
            max_attempts=3,
            backoff_base_seconds=0,
            backoff_max_seconds=0,
        ),
        circuit=CircuitBreaker(
            DependencyName.OBJECT_STORE,
            failure_threshold=5,
            recovery_seconds=1,
        ),
    )

    assert result == "ok"
    assert calls == 2


@pytest.mark.verifies("ADR-0023")
async def test_dependency_timeout_stops_at_the_attempt_budget():
    calls = 0

    async def never_returns() -> None:
        nonlocal calls
        calls += 1
        await asyncio.Event().wait()

    with pytest.raises(DependencyUnavailable):
        await call_dependency(
            DependencyName.OBJECT_STORE,
            never_returns,
            policy=DependencyPolicy(
                timeout_seconds=0.01,
                max_attempts=2,
                backoff_base_seconds=0,
                backoff_max_seconds=0,
            ),
            circuit=CircuitBreaker(
                DependencyName.OBJECT_STORE,
                failure_threshold=5,
                recovery_seconds=1,
            ),
        )

    assert calls == 2


@pytest.mark.verifies("ADR-0023")
async def test_open_circuit_fails_fast_without_calling_provider_again():
    calls = 0

    async def fails() -> None:
        nonlocal calls
        calls += 1
        raise OSError("provider unavailable")

    circuit = CircuitBreaker(
        DependencyName.OBJECT_STORE,
        failure_threshold=1,
        recovery_seconds=60,
    )
    policy = DependencyPolicy(
        timeout_seconds=1,
        max_attempts=1,
        backoff_base_seconds=0,
        backoff_max_seconds=0,
    )

    with pytest.raises(DependencyUnavailable):
        await call_dependency(
            DependencyName.OBJECT_STORE,
            fails,
            policy=policy,
            circuit=circuit,
        )
    with pytest.raises(CircuitOpen):
        await call_dependency(
            DependencyName.OBJECT_STORE,
            fails,
            policy=policy,
            circuit=circuit,
        )

    assert calls == 1


@pytest.mark.verifies("FR-SCR-013", "ADR-0025")
def test_object_store_construction_failure_has_dependency_semantics(monkeypatch):
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace())
    )
    monkeypatch.setattr(
        deps,
        "create_object_store",
        lambda _settings: (_ for _ in ()).throw(RuntimeError("SDK setup failed")),
    )

    with pytest.raises(DependencyUnavailable) as caught:
        deps.object_store_from_request(
            request,  # type: ignore[arg-type]
            Settings(VENDOR_FIXTURE_MODE=True),  # type: ignore[call-arg]
        )

    assert caught.value.dependency is DependencyName.OBJECT_STORE
    assert not hasattr(request.app.state, "object_store")
