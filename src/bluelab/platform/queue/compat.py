"""Job-payload compatibility across the N / N+1 window (pipeline/02 §3, gate F-2).

The queue is the second seam where two releases coexist: a draining N+1 worker
enqueues a job an N worker may dequeue after a rollback, and the reverse. So job
payloads follow the same expand-only discipline as the schema — a consumer
tolerates an unknown field; a new required field or a new job kind is additive,
and its handler ships before anything enqueues it.

## The failure this prevents

Without the rule, a rollback strands N+1-shaped jobs that an N worker cannot
parse. The worker fails them, they retry, they fail again — and because grading
failures are *designed* to read as "preparing" to the participant
(FR-SCR-009), nothing surfaces. **Grading silently misses its turnaround with no
error shown to anyone.** That is the precise scenario pipeline/02 §3 requires the
window test to cover in both directions.

## The envelope

Every payload is wrapped: a `v` version, the scope the worker resolves from, and
the caller's `args`. The scope lives on the envelope rather than inside the args
so the worker can establish its GUCs without understanding the lane's payload —
which is what lets an N worker scope a job it only partly understands.

Unknown envelope keys are ignored on read, by construction: `read()` names the
fields it wants and does not iterate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

ENVELOPE_VERSION: Final = 1
"""Bump only for a change no tolerant reader could absorb — which, under the
expand-only rule, should be never. A bump means both releases must ship handlers
first."""


class IncompatiblePayload(Exception):
    """A payload this release cannot safely execute.

    Raised only for a *newer* envelope version — the one case where tolerance is
    wrong. An N worker that guessed at an N+1 envelope would run the job with a
    scope it misread, and a mis-scoped worker is a cross-tenant write.

    The correct handling is to leave the job for a worker that understands it,
    not to fail it: during a rollback window, workers of both releases are
    polling the same lane.
    """


def envelope(
    payload: dict[str, Any], *, org_id: UUID, team_id: UUID | None = None
) -> dict[str, Any]:
    """Wrap a payload for the queue.

    Args:
        payload: The lane's own arguments — ids only.
        org_id: The scope the worker will set as GUCs.
        team_id: The team scope, where the job has one.
    """
    body: dict[str, Any] = {
        "v": ENVELOPE_VERSION,
        "org_id": str(org_id),
        "args": payload,
    }
    if team_id is not None:
        body["team_id"] = str(team_id)
    return body


@dataclass(frozen=True, slots=True)
class JobEnvelope:
    """A read envelope: the scope, and the lane's args."""

    version: int
    org_id: UUID
    team_id: UUID | None
    args: dict[str, Any]


def read(body: dict[str, Any]) -> JobEnvelope:
    """Read an envelope tolerantly.

    Tolerant in the expand direction — unknown keys are ignored, because a newer
    release adding a field must not break an older worker. Strict in the other:
    a *newer* envelope version is refused rather than guessed at.

    Raises:
        IncompatiblePayload: If the envelope version is newer than this release
            understands.
        ValueError: If required scope is absent — a job with no scope has no
            unscoped path to fall back to (ADR-0005), so it cannot run at all.
    """
    version = int(body.get("v", 1))
    if version > ENVELOPE_VERSION:
        raise IncompatiblePayload(
            f"envelope v{version} is newer than this release (v{ENVELOPE_VERSION}) — "
            "leave it for a worker that understands it"
        )

    raw_org = body.get("org_id")
    if raw_org is None:
        raise ValueError("job envelope carries no org_id — the work plane has no unscoped path")

    raw_team = body.get("team_id")
    return JobEnvelope(
        version=version,
        org_id=UUID(str(raw_org)),
        team_id=UUID(str(raw_team)) if raw_team is not None else None,
        args=dict(body.get("args", {})),
    )
