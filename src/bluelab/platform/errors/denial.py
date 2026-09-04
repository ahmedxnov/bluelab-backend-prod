"""Denial semantics (api/00 §4.1; AC-IDA-006 / AC-IDA-007).

Any resource the principal's scope tuple does not reach answers 404 with the
generic `not-found` problem — identical in status, body bytes, and timing to a
truly absent id. 403 exists only where the specification explicitly discloses
state: deactivated-account sign-in, and the gate-pending conditions.

Byte-and-timing equality is attacked by the L7 battery. Producing every denial
from one place is what gives that battery a single thing to attack.

## How each of the three equalities is actually achieved

**Status.** One constant, one call site: `raise not_found()`.

**Bytes.** `not_found()` takes no arguments. There is no `detail`, no resource
name, no id echo — nothing a caller could pass that would make one denial
distinguishable from another. The rendered body differs only in `request_id`,
which is per-request and carries no information about the resource. That the
function *cannot* accept a discriminating argument is the design; a `detail`
parameter with a docstring asking callers not to use it would be worthless.

**Timing.** This is the one that cannot be fixed at the response layer, and
padding with a sleep would make it worse — a constant floor is observable and a
random jitter just needs more samples. It is achieved *structurally instead*:
the authorization decision is made by RLS, inside the same query that would have
fetched the row. A cross-scope read and a genuinely absent id follow the identical
code path and do the identical work — both are "the query returned no rows."

That property holds only if callers preserve it, which is the rule below.
"""

from __future__ import annotations

from typing import NoReturn

from bluelab.platform.errors import catalog
from bluelab.platform.errors.catalog import ProblemType


class ProblemError(Exception):
    """Base for every error that renders as an RFC 9457 problem.

    Args:
        problem: The catalog entry. Its slug is the contract element the client
            branches on.
        detail: Optional secondary text. **Never** carries a resource name, an
            id, or anything that varies with what the caller asked for on a
            denial path — see `not_found`.
        meta: Extra members the designed UX state needs. `409 weights-not-100`
            carries `delta`; `409 allowance-exhausted` carries the used/allowed
            counts; `409 stage-not-next` carries `next_stage_ord`. Each one is
            named in ux/05 §3 and is part of the contract.
        headers: Response headers the problem requires — `Retry-After` on
            `429 rate-limited` and `503 call-capacity`.
    """

    def __init__(
        self,
        problem: ProblemType,
        *,
        detail: str | None = None,
        meta: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(f"{problem.slug}: {detail or problem.title}")
        self.problem = problem
        self.detail = detail
        self.meta = meta or {}
        self.headers = headers or {}


class ValidationProblem(ProblemError):
    """`422 validation-error` with field-level `errors[]`.

    Args:
        errors: One entry per offending field. The SPA binds these to controls,
            focuses the first, and shows a summary count at three or more
            (ux/05 §3.1).
    """

    def __init__(self, errors: list[dict[str, str]], *, detail: str | None = None) -> None:
        super().__init__(catalog.VALIDATION_ERROR, detail=detail, meta={"errors": errors})
        self.errors = errors


def not_found() -> ProblemError:
    """The one denial.

    Takes no arguments, deliberately: a cross-scope read and a genuinely absent
    id must be indistinguishable, and the cheapest way to guarantee that is to
    give the caller nothing to differ with. Byte-identical responses come from
    the *absence of varying fields*, not from sharing an object.

    A **fresh instance per call** is deliberate. A module-level singleton would
    be byte-identical too, and would also accumulate `__traceback__` on every
    raise — frames holding request and session objects, retained for the life of
    the process — and would race between coroutines mutating `__traceback__` and
    `__context__` on the same object.

    Use for **every** unreachable resource: another org, another team, another
    rep's self-authored drill, a candidate's forbidden review, a manager's URL
    for a route their role does not mount. Never `403` for these — 403 is a
    disclosure, and disclosing that a resource exists but is forbidden is the
    exact leak AC-IDA-006 forbids.

    Example:
        drill = await repo.get(drill_id)   # RLS already scoped the query
        if drill is None:
            raise not_found()
    """
    return ProblemError(catalog.NOT_FOUND)


def deny() -> NoReturn:
    """Raise `not_found()`. Sugar for the common single-line guard."""
    raise not_found()
