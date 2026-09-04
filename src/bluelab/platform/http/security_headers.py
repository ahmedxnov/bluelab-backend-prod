"""Security response headers (security/05 §3, OWASP A02:2025).

Set on **every** response by middleware rather than per-route, for the same
reason `no-store` is: a new endpoint that forgets to think about headers gets the
safe ones, instead of the safe ones depending on someone remembering.

## Why each header is here

`Strict-Transport-Security`
    Removes the plaintext downgrade window entirely. `includeSubDomains` matters
    specifically because the `__Host-` cookie prefix's protection assumes no
    hostile sibling subdomain can influence the origin.

`X-Content-Type-Options: nosniff`
    Stops a browser second-guessing `application/problem+json` or a presigned
    download and executing it as script.

`Content-Security-Policy: frame-ancestors 'none'`
    Clickjacking. `frame-ancestors` supersedes `X-Frame-Options`, which is still
    sent for older engines in the NFR-007 matrix. Nothing in this product is ever
    legitimately framed.

`Referrer-Policy: no-referrer`
    **The one that is product-specific rather than boilerplate.** The candidate
    invite URL carries a 256-bit capability token in its fragment. Fragments are
    not transmitted in `Referer`, so this is defence in depth — but the token
    also travels through an email client, an unfamiliar machine, and whatever
    the candidate has installed. `no-referrer` costs nothing here: the product is
    auth-gated with no outbound analytics and no SEO surface.

`Cross-Origin-Opener-Policy` / `Cross-Origin-Resource-Policy`
    Process isolation and cross-origin read blocking. Free, given the SPA and API
    are same-origin by design.

`Cache-Control: no-store`
    Every response on this surface is either customer data or an error about it.
    `GET /auth/session` alone returns a display name, an email address and the
    org — and without this header a browser, a corporate proxy or any
    intermediary may keep a copy. `no-store` rather than `no-cache`: the latter
    permits storage and only requires revalidation, which still leaves the body
    on disk. There is no endpoint here whose response is worth caching, so this
    is set unconditionally rather than per-route.

## The CSP is deliberately narrow, and deliberately not complete here

A full `script-src` policy belongs with the SPA build, which knows its own hashes
and whether it needs a nonce. Shipping a permissive `default-src` from the
backend would be worse than shipping none: it would look like coverage while
allowing `unsafe-inline`. So this sets the framing and object policy — which is
build-independent — and leaves script policy to the frontend's own headers.

`connect-src` deserves a note when that lands: the call surface opens a WebRTC
connection and a WebSocket to LiveKit, so a naive `'self'` policy breaks the
product's core feature.
"""

from __future__ import annotations

from typing import Final

from starlette.responses import Response
from starlette.types import ASGIApp

HSTS_MAX_AGE: Final = 31_536_000  # one year

BASE_HEADERS: Final[dict[str, str]] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Content-Security-Policy": "frame-ancestors 'none'; base-uri 'none'; object-src 'none'",
    "Cache-Control": "no-store",
}

HSTS_HEADER: Final = f"max-age={HSTS_MAX_AGE}; includeSubDomains"


class SecurityHeadersMiddleware:
    """Attach the security headers to every response.

    Args:
        app: The ASGI app.
        enable_hsts: False on local http, where HSTS against `localhost` would
            pin the browser to a scheme the dev server does not serve. Drive it
            from `Settings.cookie_secure` so the two transport-conditional values
            stay in agreement.
    """

    def __init__(self, app: ASGIApp, *, enable_hsts: bool) -> None:
        self.app = app
        self._headers = dict(BASE_HEADERS)
        if enable_hsts:
            self._headers["Strict-Transport-Security"] = HSTS_HEADER

    async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):  # type: ignore[no-untyped-def]
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                existing = {name.decode().lower() for name, _ in headers}
                for name, value in self._headers.items():
                    # Never override a header a route set deliberately — the
                    # call surface will need its own connect-src.
                    if name.lower() not in existing:
                        headers.append((name.encode(), value.encode()))
            await send(message)

        await self.app(scope, receive, send_with_headers)


def apply(response: Response, *, enable_hsts: bool) -> Response:
    """Attach the headers to a single response.

    For responses produced outside the middleware chain — an exception handler
    invoked before middleware, for instance.
    """
    for name, value in BASE_HEADERS.items():
        response.headers.setdefault(name, value)
    if enable_hsts:
        response.headers.setdefault("Strict-Transport-Security", HSTS_HEADER)
    return response
