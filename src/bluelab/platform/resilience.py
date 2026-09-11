"""Bounded calls to capabilities outside the BlueLab trust domain.

Every external call has three limits (architecture/04 section 2): a timeout
inside the caller's budget, a bounded retry with backoff, and a circuit breaker
that fails fast while a dependency is unhealthy.  Adapters share this module so
those properties do not drift between S3, KMS, email, and later model clients.

Only dependency names, outcomes, attempt counts, and timings reach telemetry.
Provider request/response values and exception messages never do.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

from bluelab.platform.telemetry import metrics, tracing


class DependencyName(StrEnum):
    """Low-cardinality dependency identities used by metrics and traces."""

    OBJECT_STORE = "object_store"
    KMS = "kms"
    EMAIL = "email"
    MEDIA_TRANSPORT = "media_transport"
    EVALUATOR = "evaluator"
    GENERATION = "generation"
    DOCUMENT_EXTRACTION = "document_extraction"


class DependencyUnavailable(RuntimeError):
    """A dependency did not succeed inside the bounded call policy."""

    def __init__(self, dependency: DependencyName) -> None:
        self.dependency = dependency
        super().__init__(f"{dependency.value} is unavailable")


class CircuitOpen(DependencyUnavailable):
    """The dependency circuit is open, so no provider call was attempted."""


@dataclass(frozen=True, slots=True)
class DependencyPolicy:
    """Limits for one logical dependency call."""

    timeout_seconds: float = 5.0
    max_attempts: int = 3
    backoff_base_seconds: float = 0.1
    backoff_max_seconds: float = 1.0

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("dependency timeout must be positive")
        if self.max_attempts < 1:
            raise ValueError("dependency max attempts must be at least one")
        if self.backoff_base_seconds < 0:
            raise ValueError("dependency backoff base cannot be negative")
        if self.backoff_max_seconds < self.backoff_base_seconds:
            raise ValueError("dependency backoff maximum cannot be below its base")


class CircuitBreaker:
    """Concurrency-safe closed/open/half-open circuit breaker.

    Exactly one probe is admitted after the recovery interval.  Other callers
    continue to fail fast until that probe succeeds, preventing a recovered
    provider from receiving a thundering herd.
    """

    def __init__(
        self,
        dependency: DependencyName,
        *,
        failure_threshold: int = 5,
        recovery_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("circuit failure threshold must be at least one")
        if recovery_seconds <= 0:
            raise ValueError("circuit recovery interval must be positive")
        self.dependency = dependency
        self._failure_threshold = failure_threshold
        self._recovery_seconds = recovery_seconds
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._probe_in_flight = False
        self._lock = asyncio.Lock()

    async def before_call(self) -> None:
        """Admit a closed-circuit call or the single half-open probe."""
        async with self._lock:
            if self._opened_at is None:
                return
            if self._clock() - self._opened_at < self._recovery_seconds:
                raise CircuitOpen(self.dependency)
            if self._probe_in_flight:
                raise CircuitOpen(self.dependency)
            self._probe_in_flight = True

    async def succeeded(self) -> None:
        """Close the circuit after any successful call or probe."""
        async with self._lock:
            self._failures = 0
            self._opened_at = None
            self._probe_in_flight = False

    async def failed(self) -> None:
        """Count a failure and open the circuit at the configured threshold."""
        async with self._lock:
            self._probe_in_flight = False
            self._failures += 1
            if self._failures >= self._failure_threshold:
                self._opened_at = self._clock()


RetryPredicate = Callable[[Exception], bool]


async def call_dependency[T](
    dependency: DependencyName,
    operation: Callable[[], Awaitable[T]],
    *,
    policy: DependencyPolicy,
    circuit: CircuitBreaker,
    retryable: RetryPredicate | None = None,
) -> T:
    """Run one logical dependency call inside all three resilience bounds.

    The provider exception is deliberately replaced by a content-free public
    error after the final attempt.  A caller that needs domain-specific handling
    should map expected, non-retryable provider errors inside ``operation``.
    """
    started = time.monotonic()
    attempts = 0
    predicate = retryable or (lambda _error: True)

    with tracing.dependency_call(dependency=dependency.value) as span:
        for attempt in range(policy.max_attempts):
            attempts = attempt + 1
            try:
                await circuit.before_call()
                async with asyncio.timeout(policy.timeout_seconds):
                    value = await operation()
            except CircuitOpen:
                _record_dependency(
                    dependency, "circuit_open", started=started, attempts=attempts, span=span
                )
                raise
            except TimeoutError:
                await circuit.failed()
            except Exception as exc:
                if not predicate(exc):
                    _record_dependency(
                        dependency,
                        "rejected",
                        started=started,
                        attempts=attempts,
                        span=span,
                    )
                    raise
                await circuit.failed()
            else:
                await circuit.succeeded()
                _record_dependency(
                    dependency, "success", started=started, attempts=attempts, span=span
                )
                return value

            if attempts < policy.max_attempts:
                delay = min(
                    policy.backoff_base_seconds * (2**attempt),
                    policy.backoff_max_seconds,
                )
                if delay:
                    await asyncio.sleep(delay)

        _record_dependency(
            dependency, "unavailable", started=started, attempts=attempts, span=span
        )
        raise DependencyUnavailable(dependency) from None


def _record_dependency(
    dependency: DependencyName,
    outcome: str,
    *,
    started: float,
    attempts: int,
    span: tracing.DependencyCallSpan,
) -> None:
    duration_ms = (time.monotonic() - started) * 1000
    span.finish(outcome=outcome, attempts=attempts)
    metrics.record_dependency(
        dependency=dependency.value,
        outcome=outcome,
        duration_ms=duration_ms,
    )
