"""The correlation spine of observability/01 §3 — `trace_id`, `org_id`,
`attempt_id`, `call_id`, `job_id`, `job_lane`, `participant_kind`, `plane`,
`tier`, `deploy_digest` — and the §3.1 cardinality rule deciding which of them
may ride a metric label and which live only on a span.

Synthetic traffic is labelled here and excluded from production SLIs (§3.2).

## The cardinality rule, enforced rather than remembered

A metric label multiplies time series. `org_id` has tens of values and is safe;
`attempt_id` has thousands per month and would create a new series per attempt —
which is how a metrics bill becomes the largest line item and a dashboard stops
loading. But `attempt_id` is exactly what makes a *trace* useful, because it is
**the join key** between call, cost, grade, review, and ops-fault.

So the split is: low-cardinality keys may be metric labels; high-cardinality keys
live on spans and logs only. `metric_labels()` returns only the former, and the
sets below are the authority — `platform.telemetry.metrics` cannot label a metric
with anything else because it never sees the rest.

## Content-free, structurally

Nothing in this module holds text. The correlation keys are identifiers, timings,
and enumerated classes — never transcript text, quoted speech, persona material,
product facts, or a person's name (observability/01 §5, SEC-024). That is what
makes it safe for a small team to operate this product without staff reading
customer content, and the enforcement scan checks it rather than trusting it.

`participant_kind` is in the set precisely because it segments a signal by
journey **without identifying the person**.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Final
from uuid import UUID, uuid4

LOW_CARDINALITY_KEYS: Final = frozenset(
    {"plane", "tier", "deploy_digest", "participant_kind", "job_lane", "org_id", "synthetic"}
)
"""Safe as metric labels (observability/01 §3.1)."""

HIGH_CARDINALITY_KEYS: Final = frozenset(
    {"trace_id", "request_id", "attempt_id", "call_id", "job_id", "account_id", "candidate_id"}
)
"""Spans and logs only. `attempt_id` is the join key — valuable, and unbounded."""


class ParticipantKind(StrEnum):
    """Role class, for segmenting a signal without identifying a person."""

    REP = "rep"
    AUTHOR = "author"
    CANDIDATE = "candidate"


@dataclass(frozen=True, slots=True)
class Correlation:
    """The identifiers in scope for the current unit of work.

    A unit of work is one request, one job, or one sweep pass. Nothing here
    survives it.
    """

    request_id: str
    plane: str
    deploy_digest: str | None = None

    org_id: UUID | None = None
    account_id: UUID | None = None
    candidate_id: UUID | None = None
    attempt_id: UUID | None = None
    call_id: UUID | None = None
    job_id: str | None = None
    job_lane: str | None = None
    participant_kind: ParticipantKind | None = None

    synthetic: bool = False
    """True for the synthetic-journey checks.

    Labelled at the source and **excluded from production SLIs** (gate FS-7): a
    synthetic caller that runs every minute would otherwise dominate the very
    percentiles it exists to watch, and a real outage would hide behind a healthy
    robot.
    """

    def metric_labels(self) -> dict[str, str]:
        """Only the low-cardinality keys. The rest are not offered."""
        labels: dict[str, str] = {"plane": self.plane, "synthetic": str(self.synthetic).lower()}
        if self.deploy_digest is not None:
            labels["deploy_digest"] = self.deploy_digest
        if self.org_id is not None:
            labels["org_id"] = str(self.org_id)
        if self.job_lane is not None:
            labels["job_lane"] = self.job_lane
        if self.participant_kind is not None:
            labels["participant_kind"] = self.participant_kind.value
        return labels

    def span_attributes(self) -> dict[str, str]:
        """Everything, high-cardinality included — spans are not time series."""
        attributes = self.metric_labels()
        attributes["request_id"] = self.request_id
        for key, value in (
            ("attempt_id", self.attempt_id),
            ("call_id", self.call_id),
            ("account_id", self.account_id),
            ("candidate_id", self.candidate_id),
        ):
            if value is not None:
                attributes[key] = str(value)
        if self.job_id is not None:
            attributes["job_id"] = self.job_id
        return attributes


_correlation: ContextVar[Correlation | None] = ContextVar("bluelab_correlation", default=None)


def new_request_id() -> str:
    """A fresh request id.

    UUIDv4, not v7: this one is quoted back to users as a support code and is not
    a database key, so there is no reason for it to encode a timestamp.
    """
    return str(uuid4())


def bind(correlation: Correlation) -> None:
    """Set the correlation for this unit of work."""
    _correlation.set(correlation)


def enrich(**fields: object) -> None:
    """Add identifiers that only become known mid-unit.

    `attempt_id` is the usual case: admission mints it partway through T-1, and
    every signal after that point should carry it.
    """
    current = _correlation.get()
    if current is None:
        return
    _correlation.set(replace(current, **fields))  # type: ignore[arg-type]


def current() -> Correlation | None:
    """The correlation in scope, or None outside a unit of work."""
    return _correlation.get()


def current_request_id() -> str:
    """The request id, or a fresh one if called outside a bound unit of work.

    Never returns empty: every problem response carries a support code, including
    one produced by a failure early enough that binding never happened.
    """
    correlation = _correlation.get()
    return correlation.request_id if correlation is not None else new_request_id()
