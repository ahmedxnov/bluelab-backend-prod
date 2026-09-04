"""The application- and work-plane metric catalogue of observability/01 §4.2-§4.4:

    http.request                    rate / error class / duration by route
    admission.wall_ms               the T-1 admission-transaction latency (R-17)
    auth.signal                     failed sign-in, tripped throttle, token flood
    candidate.entry_probe           pre-flight / token abuse attempts
    job.queue_depth, job.oldest_age per lane
    grading.turnaround_ms           call end -> review available (NFR-005)
    job.result                      success / retry / ops-fault raised
    model.version                   vendor-served model or alias id
    email.delivery                  incl. sent-without-terminal-event age
    vendor.spend_fraction           month-to-date spend / cap (the 80% warning)
    ops.fault_queue, ops.audit, ops.break_glass

## Why the instruments are named constants and the emitters are functions

Two reasons, both about not being able to get it wrong later:

* **Labels come from the correlation spine, never from the call site.** Every
  emitter takes its labels from `correlation.metric_labels()`, which returns only
  the low-cardinality keys. A caller cannot label a metric with `attempt_id`
  because it is never offered one (observability/01 §3.1).
* **The catalogue is enumerable.** Dashboards and alert rules are written against
  these names, so a rename is a change to an operational contract, not a
  refactor.

`admission.wall_ms` deserves its own note: it is the T-1 transaction's wall time
and the **R-17 migration trigger**. Admission is user-facing latency — the
participant is waiting — and it is the one product query whose cost is dominated
by the round trip to the data layer. It is a first-class signal, not a debugging
aid.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from opentelemetry import metrics

from bluelab.platform.telemetry import correlation

_meter: Final = metrics.get_meter("bluelab.backend")

# ── application plane (RED) ───────────────────────────────────────────────────
http_request_duration = _meter.create_histogram(
    "http.request.duration", unit="ms", description="Product-surface request duration by route"
)
admission_wall_ms = _meter.create_histogram(
    "admission.wall_ms",
    unit="ms",
    description="T-1 admission-transaction latency — the R-17 migration trigger",
)
auth_signal = _meter.create_counter(
    "auth.signal", description="Failed sign-in, tripped throttle, invalid-token flood (SEC-004/005)"
)
candidate_entry_probe = _meter.create_counter(
    "candidate.entry_probe", description="Pre-flight and token abuse attempts (SEC-007)"
)

# ── work plane (RED + queue depth) ────────────────────────────────────────────
job_result = _meter.create_counter(
    "job.result", description="success | retry | ops_fault, per lane"
)
job_duration = _meter.create_histogram(
    "job.duration", unit="ms", description="Job execution time, per lane"
)
grading_turnaround_ms = _meter.create_histogram(
    "grading.turnaround_ms",
    unit="ms",
    description="Call end to review available — NFR-005 (p50 20s / p95 45s)",
)
email_delivery = _meter.create_counter(
    "email.delivery", description="sent | delivered | bounced | delayed, by template kind"
)

# ── operations ────────────────────────────────────────────────────────────────
ops_break_glass = _meter.create_counter(
    "ops.break_glass", description="Every break-glass use — pages on any value (SEC-014)"
)


class AuthSignal(StrEnum):
    """The enumerated auth signals. A closed set keeps the label bounded."""

    SIGN_IN_FAILED = "sign_in_failed"
    THROTTLE_TRIPPED = "throttle_tripped"
    TOKEN_INVALID = "token_invalid"
    SESSION_REVOKED = "session_revoked"


class JobOutcome(StrEnum):
    """`job.result` values. `OPS_FAULT` is retry exhaustion, not a retry."""

    SUCCESS = "success"
    RETRY = "retry"
    OPS_FAULT = "ops_fault"


def _labels(**extra: str) -> dict[str, str]:
    """Correlation labels plus the caller's, low-cardinality only."""
    current = correlation.current()
    labels = current.metric_labels() if current is not None else {}
    labels.update(extra)
    return labels


def record_http_request(*, route: str, status: int, duration_ms: float) -> None:
    """One product-surface request.

    `route` is the *template* (`/api/v1/drills/{drill_id}`), never the resolved
    path — a path would put an id into a metric label and multiply the series per
    drill.
    """
    http_request_duration.record(
        duration_ms, _labels(route=route, status_class=f"{status // 100}xx")
    )


def record_admission(*, duration_ms: float, outcome: str) -> None:
    """One T-1 admission transaction (placed, refused, or failed)."""
    admission_wall_ms.record(duration_ms, _labels(outcome=outcome))


def record_auth_signal(signal: AuthSignal) -> None:
    """One authentication signal (SEC-004 / SEC-005)."""
    auth_signal.add(1, _labels(signal=signal.value))


def record_candidate_probe(*, kind: str) -> None:
    """One candidate entry probe — token or pre-flight abuse (SEC-007)."""
    candidate_entry_probe.add(1, _labels(probe=kind))


def record_job(*, lane: str, outcome: JobOutcome, duration_ms: float) -> None:
    """One job execution.

    An `OPS_FAULT` outcome is retry exhaustion — the point where a grading
    failure becomes an ops-fault record rather than another attempt
    (FR-SCR-009).
    """
    labels = _labels(job_lane=lane, outcome=outcome.value)
    job_result.add(1, labels)
    job_duration.record(duration_ms, labels)


def record_grading_turnaround(*, duration_ms: float) -> None:
    """Call end to review available — the NFR-005 SLI."""
    grading_turnaround_ms.record(duration_ms, _labels())


def record_email_delivery(*, kind: str, state: str) -> None:
    """One email delivery-state transition.

    A silent drop is caught by the **absence** of a terminal event rather than by
    an error, so the sent-without-terminal-event age is watched separately by the
    alert rule — this counter is what it ages against (gate FS-10).
    """
    email_delivery.add(1, _labels(email_kind=kind, state=state))


def record_break_glass(*, verb: str) -> None:
    """One break-glass use. Pages on any value at all (SEC-014)."""
    ops_break_glass.add(1, _labels(verb=verb))
