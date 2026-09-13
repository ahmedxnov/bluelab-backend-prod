"""Content-free tracing emitters for bounded dependency calls.

Application and adapter code use this module instead of importing
OpenTelemetry directly.  The small handle exposes only the closed dependency
attributes that are permitted by the observability contract.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Final

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

_dependency_tracer: Final = trace.get_tracer("bluelab.backend.dependencies")


@dataclass(frozen=True, slots=True)
class DependencyCallSpan:
    """A dependency span restricted to low-cardinality outcome metadata."""

    _span: Span

    def finish(self, *, outcome: str, attempts: int) -> None:
        """Record the terminal bounded outcome and number of attempts."""
        self._span.set_attribute("dependency.outcome", outcome)
        self._span.set_attribute("dependency.attempts", attempts)
        if outcome != "success":
            self._span.set_status(Status(StatusCode.ERROR))


@contextmanager
def dependency_call(*, dependency: str) -> Iterator[DependencyCallSpan]:
    """Create a span carrying only the closed adapter-level dependency name."""
    with _dependency_tracer.start_as_current_span("dependency.call") as span:
        span.set_attribute("dependency.name", dependency)
        yield DependencyCallSpan(span)
