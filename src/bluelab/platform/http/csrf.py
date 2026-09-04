"""The CSRF stance of api/00 §3: `SameSite=Strict` cookies on a same-origin SPA,
with rejection of unsafe-method requests whose `Origin` / `Sec-Fetch-Site`
indicate a cross-site initiator as the second belt. No CSRF tokens. Candidate
bearer auth carries no ambient credential and needs no defence.

## Why no tokens

A CSRF token defends a cookie that a browser will attach to a cross-site request.
`SameSite=Strict` means the browser will not attach it at all — the attack the
token defends against cannot reach the server. Adding tokens anyway would mean a
token endpoint, token rotation, and a class of "invalid CSRF token" errors that
users hit for reasons unrelated to attacks.

At Tier 1 the same-origin property is *stronger* than usual: Caddy serves the SPA
and `/api/v1` from one hostname, one process, one TLS terminator — so there is no
`X-Forwarded-Proto` to trust incorrectly (infra/01 §7.2 position 2).

## What this check is actually for

Defence in depth against a browser that does not implement `SameSite`, or a
future same-site subdomain that turns out not to be as trusted as assumed. It
fails **closed on ambiguity in production**: if neither `Sec-Fetch-Site` nor
`Origin` is present on an unsafe method, the request is refused. Absent headers
mean a non-browser client, and no non-browser client is supposed to be holding a
session cookie.
"""

from __future__ import annotations

from typing import Final

from starlette.requests import Request

SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

_SAFE_FETCH_SITES: Final = frozenset({"same-origin", "same-site", "none"})
"""`none` is a user-initiated navigation — typing a URL, a bookmark — not a
cross-site request."""


def is_cross_site_write(request: Request, *, allowed_origin: str | None) -> bool:
    """True if this looks like a cross-site initiator on an unsafe method.

    Args:
        request: The incoming request.
        allowed_origin: The application's own origin, e.g.
            `https://app.bluelab.ai`. When None, the `Origin` fallback is skipped
            and only `Sec-Fetch-Site` is consulted.

    Returns:
        True when the request should be refused.
    """
    if request.method in SAFE_METHODS:
        return False

    # Bearer-authenticated candidates carry no ambient credential: a cross-site
    # page cannot make the browser attach a header it does not know.
    if request.headers.get("authorization"):
        return False

    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None:
        return fetch_site not in _SAFE_FETCH_SITES

    origin = request.headers.get("origin")
    if origin is not None and allowed_origin is not None:
        return origin.rstrip("/") != allowed_origin.rstrip("/")

    # Neither header present on an unsafe, cookie-authenticated request.
    return _has_session_cookie(request)


def _has_session_cookie(request: Request) -> bool:
    """True if the request carries one of our ambient credentials.

    A request with no session cookie has nothing for CSRF to abuse, so refusing
    it would only break legitimate non-browser callers (health checks, the
    signed internal seam) for no security gain.
    """
    return any(name.startswith("__Host-bluelab") for name in request.cookies)
