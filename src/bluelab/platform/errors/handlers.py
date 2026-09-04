"""Exception handlers registered on the app. Validation failures carry field-level
`errors[]`; every response carries `request_id` for support correlation.

## The two rules these handlers exist to enforce centrally

**Nothing leaks through an unhandled exception.** The catch-all renders
`500 internal-error` with a `request_id` and *no detail* — a stack trace, an
exception message, or a database error string in a response body is an
information leak, and the one that most often carries a table or column name.
The full exception goes to the log, correlated by the same `request_id`.

**FastAPI's defaults are replaced, not supplemented.** Out of the box, FastAPI
renders `{"detail": ...}` for `HTTPException` and a bespoke shape for
`RequestValidationError`. Neither is RFC 9457, and a surface that emits two error
shapes forces the client to branch on more than `type` — which ux/05 §3 forbids.
So both are overridden.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from bluelab.platform.errors import catalog
from bluelab.platform.errors.catalog import CONTENT_TYPE, ProblemType
from bluelab.platform.errors.denial import ProblemError
from bluelab.platform.telemetry.correlation import current_request_id

_log = logging.getLogger("bluelab.errors")


def render_problem(
    problem: ProblemType,
    *,
    request_id: str,
    detail: str | None = None,
    meta: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Render one RFC 9457 problem document.

    Members beyond the RFC's five are extension members, which the RFC permits
    and the contract uses for the designed UX states (`delta`, `next_stage_ord`,
    the allowance counts).
    """
    body: dict[str, Any] = {
        "type": problem.type_uri,
        "title": problem.title,
        "status": problem.status,
        "request_id": request_id,
    }
    if detail is not None:
        body["detail"] = detail
    if meta:
        body.update(meta)

    return JSONResponse(
        status_code=problem.status,
        content=body,
        media_type=CONTENT_TYPE,
        headers=headers,
    )


async def handle_problem(request: Request, exc: Exception) -> Response:
    """Render a `ProblemError` — the intended path for every expected failure.

    ## Why `not-found` is logged and the rest are not

    `not_found()` is the whole authorization surface for customer data: a role
    reaching a route it does not mount, a scope reaching another team's row. The
    response is deliberately indistinguishable from a genuine absence, which is
    the point — and it means that without this line, a principal walking the
    surface to see what exists leaves no trace anywhere. ADR-0036 accepts the
    debuggability cost of the 404 policy on the grounds that "ops-side logs
    retain the real cause"; this is that retention.

    The other problems are not logged here. A gate refusal, a `409` conflict or a
    `422` is the product working — the client is told exactly what happened and
    routes the user accordingly — so logging them would bury the one line that
    carries security signal under everything a client bug can generate.

    ## What the line carries, and what it cannot carry yet

    Identifiers and timings only (SEC-024, observability/01 §5): the request id,
    the method, the path. No detail, no `meta`, no body — a denial's `meta` is
    empty by construction, but the rule is the rule.

    **It does not name the principal**, and that is a limitation rather than a
    choice. `Correlation` has an `account_id` field, but `correlation.bind` is
    called only from `platform/queue/context.py` — the job path — so on an HTTP
    request `correlation.current()` is `None` and there is nothing to read. Until
    a request binds one, ops can see the shape of the probing and tie any single
    line to the support code the caller was shown; attributing a campaign to an
    account needs that plumbing first.

    `request_id` is resolved once and shared with the response deliberately: the
    two are the same value, which is what makes the log line reachable from a
    support ticket at all.

    **This must not change the response.** Denial and absence stay byte-identical
    apart from `request_id` (AC-IDA-006); what differs is only what ops can see
    afterwards, which is a surface the caller never observes.
    """
    if not isinstance(exc, ProblemError):  # pragma: no cover — registration guarantees it
        return await handle_unexpected(request, exc)
    request_id = current_request_id()
    if exc.problem is catalog.NOT_FOUND:
        _log.warning(
            "not_found",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
            },
        )
    meta = dict(exc.meta)
    return render_problem(
        exc.problem,
        request_id=request_id,
        detail=exc.detail,
        meta=meta,
        headers=exc.headers,
    )


async def handle_validation(request: Request, exc: Exception) -> Response:
    """Render FastAPI's own validation failure as `422 validation-error`.

    Pydantic's raw error list carries the input value and an internal location
    tuple. Neither belongs in a response: the value may be a password or a token,
    and the location leaks the internal model shape. Only the field path and a
    message survive.
    """
    if not isinstance(exc, RequestValidationError):  # pragma: no cover
        return await handle_unexpected(request, exc)
    errors = [
        {
            "field": ".".join(str(part) for part in error["loc"][1:]) or str(error["loc"][0]),
            "message": error["msg"],
        }
        for error in exc.errors()
    ]
    return render_problem(
        catalog.VALIDATION_ERROR,
        request_id=current_request_id(),
        meta={"errors": errors},
    )


async def handle_http_exception(request: Request, exc: Exception) -> Response:
    """Map a raw `HTTPException` onto the catalog.

    Starlette raises these itself — 404 for an unmatched route, 405 for a wrong
    method — so they cannot simply be forbidden. An unmatched route must render
    as **the one 404 surface**, identical to a scope denial: a route that does
    not exist and a resource the principal cannot reach are the same answer.
    """
    if not isinstance(exc, StarletteHTTPException):  # pragma: no cover
        return await handle_unexpected(request, exc)

    explicit = {
        429: catalog.RATE_LIMITED,
        503: catalog.SERVICE_UNAVAILABLE,
        413: catalog.FILE_TOO_LARGE,
        415: catalog.UNSUPPORTED_FILE_TYPE,
    }.get(exc.status_code)

    if explicit is not None:
        problem = explicit
    elif exc.status_code < 500:
        # Everything else Starlette can raise on the 4xx side — 404 unmatched
        # route, **405 wrong method**, 406, 501 — renders as the one 404 surface.
        # A 405 would disclose that the path exists and only the method is wrong,
        # which is the same leak as disclosing that a resource exists: it lets a
        # caller map the surface by probing methods (api/00 §4.1, ADR-0036).
        problem = catalog.NOT_FOUND
    else:
        problem = catalog.INTERNAL_ERROR
        _log.warning(
            "unmapped_http_exception",
            extra={"status": exc.status_code, "request_id": current_request_id()},
        )

    return render_problem(problem, request_id=current_request_id())


async def handle_unexpected(request: Request, exc: Exception) -> Response:
    """The catch-all: `500 internal-error`, no detail, full trace to the log."""
    request_id = current_request_id()
    _log.exception("unhandled_exception", extra={"request_id": request_id})
    return render_problem(catalog.INTERNAL_ERROR, request_id=request_id)


def register(app: FastAPI) -> None:
    """Install every handler. Called once from the application factory."""
    app.add_exception_handler(ProblemError, handle_problem)
    app.add_exception_handler(RequestValidationError, handle_validation)
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)
    app.add_exception_handler(Exception, handle_unexpected)
