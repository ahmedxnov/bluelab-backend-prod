"""Cache-Control policy. `no-store` by default, because this is customer data. The
only cacheable responses are the public legal documents and the *published*
drill brief and reference — frozen content, where invalidation cannot arise
(api/00 §2, ADR-0007).

## The default is the safe one, and it is applied by middleware

`no-store` is set on every response and then *removed* by the two endpoints that
opt in. Fail-safe in the right direction: a new endpoint that forgets to think
about caching is uncacheable, rather than a new endpoint accidentally letting a
shared proxy hold someone's scorecard.

## Why exactly two exceptions

Published drill content is **immutable** — changing content means a new drill
(FR-DRL-015) — so invalidation cannot arise, which is the only reason caching is
safe here at all. It is also the pre-call moment where latency reads as
fragility, so the SPA holds it with `staleTime: Infinity` (ux/07 §6).

`private` matters on that one: the brief is frozen but it is still one team's
content, and a shared cache holding it would cross a tenant boundary even though
the bytes never change.
"""

from __future__ import annotations

from typing import Final

from fastapi import Response

NO_STORE: Final = "no-store"
"""The default for every response carrying customer data."""

FROZEN_CONTENT: Final = "private, max-age=3600, immutable"
"""Published brief and reference. `private` keeps it out of shared caches."""

PUBLIC_REFERENCE: Final = "public, max-age=3600"
"""`/legal-documents` only — genuinely public reference material."""


def no_store(response: Response) -> Response:
    """Mark a response uncacheable. The default; middleware applies it."""
    response.headers["Cache-Control"] = NO_STORE
    return response


def frozen(response: Response) -> Response:
    """Mark a response as immutable frozen content.

    Only valid for a **published** drill's brief or reference. Applying it to a
    draft would cache content that is still changing, and the author would keep
    seeing a stale scenario after regenerating it.
    """
    response.headers["Cache-Control"] = FROZEN_CONTENT
    return response


def public_reference(response: Response) -> Response:
    """Mark a response as public reference material (`/legal-documents`)."""
    response.headers["Cache-Control"] = PUBLIC_REFERENCE
    return response
