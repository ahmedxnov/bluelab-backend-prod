"""The closed job catalogue of api/02 §2 — the lanes, their payload shapes, their
idempotency identities, and their failure surfaces.

Payloads carry **ids only, never content**: every worker re-reads current truth
from the store, which is what makes at-least-once retry safe.

## The catalogue is closed, and enforced closed

Six lanes exist. `enqueue()` rejects a name that is not one of them, the same way
the email dispatcher rejects a `kind` outside the inventory of five. A queue that
accepts arbitrary job names grows a seventh lane nobody designed, with no
idempotency identity and no failure surface.

## Ids only — the rule that makes retry safe

If a payload carried content, a retry would apply *stale* content: the worker
would act on what was true when the job was enqueued rather than what is true
now. With ids only, a retry re-reads and converges. It is also why erasure works
— a job in flight for an erased subject reads the erased state rather than
replaying a copy of the person from a queue row (ADR-0033).

## The failure surface is part of the contract

Each lane's exhaustion behaviour is specified, and they differ on purpose.
Grading raises an ops-fault and **never voids the call** (FR-SCR-009); generation
sets a status with a reason and blocks publish, with **no fallback content**
(FR-DRL-006); extraction leaves the live facts untouched (FR-KNW-008). A generic
"mark failed and alert" would violate all three.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class Lane(StrEnum):
    """The six job lanes. The set is closed (api/02 §2)."""

    GRADE_ATTEMPT = "grade_attempt"
    GENERATE_SCENARIO = "generate_scenario"
    GENERATE_RUBRIC = "generate_rubric"
    EXTRACT_FACTS = "extract_facts"
    RENDER_REPORT = "render_report"
    DISPATCH_EMAIL = "dispatch_email"


@dataclass(frozen=True, slots=True)
class LaneSpec:
    """One lane's contract."""

    lane: Lane
    payload_keys: frozenset[str]
    """Required payload keys. Ids only — the check is mechanical, not advisory."""

    idempotency: str
    """What makes a second execution a no-op. Prose, because the mechanism differs
    per lane: a unique constraint, a status transition, a request-id match."""

    max_attempts: int
    """Retries before the lane's exhaustion behaviour fires."""

    retry_exhausted: str
    """What happens on exhaustion — specified per lane, never generic."""


SPECS: Final[dict[Lane, LaneSpec]] = {
    Lane.GRADE_ATTEMPT: LaneSpec(
        lane=Lane.GRADE_ATTEMPT,
        payload_keys=frozenset({"attempt_id"}),
        idempotency=(
            "scorecard.attempt_id unique — T-3 inserts ON CONFLICT DO NOTHING, so a "
            "second grade is a silent no-op (FR-SCR-003)"
        ),
        max_attempts=5,
        retry_exhausted=(
            "insert ops_fault(kind='grading_failure'). NEVER voids the call. While "
            "failing, attempt.status='grading_pending' and the participant reads "
            "'preparing', never an error (FR-SCR-009)"
        ),
    ),
    Lane.GENERATE_SCENARIO: LaneSpec(
        lane=Lane.GENERATE_SCENARIO,
        payload_keys=frozenset({"drill_id", "request_id"}),
        idempotency=(
            "request_id — one per author click; the worker writes only if the drill's "
            "pending request still matches, so a stale generation never overwrites a newer one"
        ),
        max_attempts=3,
        retry_exhausted=(
            "drill.generation.scenario_status='failed' + reason. NO FALLBACK CONTENT; "
            "publish stays blocked. Retry is the author re-requesting (FR-DRL-006)"
        ),
    ),
    Lane.GENERATE_RUBRIC: LaneSpec(
        lane=Lane.GENERATE_RUBRIC,
        payload_keys=frozenset({"drill_id", "request_id"}),
        idempotency="request_id, as generate_scenario; replaces the rubric wholesale on success",
        max_attempts=3,
        retry_exhausted="as generate_scenario (FR-DRL-011)",
    ),
    Lane.EXTRACT_FACTS: LaneSpec(
        lane=Lane.EXTRACT_FACTS,
        payload_keys=frozenset({"upload_id"}),
        idempotency=(
            "document_upload.status transition received->extracting->extracted|failed; "
            "re-running a terminal upload is a no-op"
        ),
        max_attempts=3,
        retry_exhausted=(
            "status='failed' + reason. Nothing reaches review and the LIVE FACTS ARE "
            "UNTOUCHED. Retry is a new upload (FR-KNW-008)"
        ),
    ),
    Lane.RENDER_REPORT: LaneSpec(
        lane=Lane.RENDER_REPORT,
        payload_keys=frozenset({"candidate_id"}),
        idempotency=(
            "candidate_report upsert keyed on candidate_id; the PDF render is "
            "conditional on pdf_status in (none, failed)"
        ),
        max_attempts=3,
        retry_exhausted=(
            "pdf_status='failed'. The report view still renders from data — the PDF is "
            "an artifact of the report, not the report (FR-HIR-011/012)"
        ),
    ),
    Lane.DISPATCH_EMAIL: LaneSpec(
        lane=Lane.DISPATCH_EMAIL,
        payload_keys=frozenset({"email_send_id"}),
        idempotency=(
            "email_send (kind, dedupe_key) unique — at most one send per "
            "(recipient, event)"
        ),
        max_attempts=5,
        retry_exhausted=(
            "email_send.status='failed'. E-2 delivery state feeds the pipeline view, so "
            "a candidate who never received the invite stays distinguishable from one "
            "who ignored it (FR-HIR-010)"
        ),
    ),
}


class UnknownLane(Exception):
    """A job name outside the closed catalogue. The queue refuses it."""


def spec_for(lane: str | Lane) -> LaneSpec:
    """Look up a lane's contract.

    Raises:
        UnknownLane: If the name is not one of the six.
    """
    try:
        return SPECS[Lane(lane)]
    except ValueError as exc:
        raise UnknownLane(f"{lane!r} is not one of {[member.value for member in Lane]}") from exc


def validate_payload(lane: Lane, payload: dict[str, object]) -> None:
    """Assert a payload carries its lane's required keys, and ids only.

    Raises:
        ValueError: If a required key is missing, or a value is not id-shaped.
            The "ids only" rule is checked rather than trusted: passing content
            through the queue is the mistake that makes retries unsafe, and it is
            invisible until a retry applies stale data.
    """
    spec = SPECS[lane]
    missing = spec.payload_keys - payload.keys()
    if missing:
        raise ValueError(f"{lane.value}: payload missing {sorted(missing)}")

    for key, value in payload.items():
        if isinstance(value, str) and len(value) > 128:
            raise ValueError(
                f"{lane.value}: payload key {key!r} looks like content, not an id — "
                "payloads carry ids only (api/02 §2)"
            )
