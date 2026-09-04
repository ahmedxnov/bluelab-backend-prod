"""Cookie construction and the startup attribute audit (SEC-002):
`__Host-` prefix, `HttpOnly`, `Secure`, `SameSite=Strict`, `Path=/`, no `Domain`.
Audited at startup and scanned in the commit gate (pipeline/02 §2 row 9).

## What each attribute is actually preventing

* **`__Host-` prefix** — the browser refuses the cookie unless it is `Secure`,
  has `Path=/`, and has **no `Domain`**. That last one is the point: without it,
  a compromised or hostile subdomain could set a cookie the app would accept. The
  prefix makes the browser enforce what we would otherwise only assert.
* **`HttpOnly`** — JavaScript cannot read it, so an XSS does not directly yield
  the session. (It is not a substitute for output encoding, SEC-020.)
* **`Secure`** — no plaintext transmission.
* **`SameSite=Strict`** — the reason there are no CSRF tokens (api/00 §3).
* **`Path=/`** — required by the prefix, and correct anyway: one origin serves
  both the SPA and the API.

## The audit runs at startup, not only in CI

The commit-gate scan reads the source; the startup audit reads the *constructed*
cookie. Those catch different mistakes — a value assembled at runtime from
configuration passes a source scan and can still be wrong. Auditing at startup
means a misconfigured deployment fails to boot rather than serving one insecure
cookie per sign-in until someone notices.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from fastapi import Response

SESSION_COOKIE: Final = "__Host-bluelab_session"
OPS_SESSION_COOKIE: Final = "__Host-bluelab_ops_session"
"""Separate cookie for a separate surface with separate authentication
(ADR-0010) — the ops session is not a customer session with extra rights."""

SAME_SITE: Final = "strict"
COOKIE_PATH: Final = "/"


class CookiePolicyViolation(Exception):
    """A cookie would be set with attributes SEC-002 forbids. Fatal at startup."""


@dataclass(frozen=True, slots=True)
class CookieSpec:
    """One cookie's attributes, validated on construction."""

    name: str
    max_age: int
    secure: bool = True
    http_only: bool = True
    same_site: str = SAME_SITE
    path: str = COOKIE_PATH
    domain: str | None = None

    def __post_init__(self) -> None:
        audit(self)


def audit(spec: CookieSpec) -> None:
    """Assert SEC-002 on one cookie spec.

    Raises:
        CookiePolicyViolation: On any deviation. Deliberately fatal: a cookie
            that fails this is a session that can be stolen, and booting anyway
            would trade a loud failure for a silent one.
    """
    problems: list[str] = []

    if not spec.http_only:
        problems.append("HttpOnly is required")
    if spec.same_site != SAME_SITE:
        problems.append(f"SameSite must be {SAME_SITE!r}, got {spec.same_site!r}")
    if spec.path != COOKIE_PATH:
        problems.append(f"Path must be {COOKIE_PATH!r}, got {spec.path!r}")
    if spec.domain is not None:
        problems.append("Domain must be absent — the __Host- prefix forbids it")

    if spec.name.startswith("__Host-"):
        # The browser enforces these for prefixed cookies; failing here gives a
        # clear message instead of a cookie the browser silently drops.
        if not spec.secure:
            problems.append("__Host- prefix requires Secure")
    elif spec.name.startswith("__Secure-"):
        if not spec.secure:
            problems.append("__Secure- prefix requires Secure")
    else:
        problems.append(f"session cookies must carry the __Host- prefix, got {spec.name!r}")

    if problems:
        raise CookiePolicyViolation(f"{spec.name}: " + "; ".join(problems))


def set_session_cookie(response: Response, spec: CookieSpec, value: str) -> None:
    """Write a validated session cookie onto a response.

    `secure` is **not** a parameter. It was, and that was the bug: `audit()`
    validated `spec.secure` while the emitted cookie took a separate runtime
    argument, so the audit could pass on a cookie that went out non-Secure — and
    with a `__Host-` prefix the browser then rejects it outright, producing
    silent session loss that the audit existed to prevent.

    The attribute now lives on the spec, so what is audited is what is sent.
    Build the spec from `Settings.cookie_secure` (see `session_spec`).
    """
    audit(spec)
    response.set_cookie(
        key=spec.name,
        value=value,
        max_age=spec.max_age,
        httponly=spec.http_only,
        secure=spec.secure,
        samesite="strict",
        path=spec.path,
    )


def clear_session_cookie(response: Response, spec: CookieSpec) -> None:
    """Expire a session cookie on sign-out.

    The server-side record is deleted separately and is what actually ends the
    session — clearing the cookie is courtesy, not enforcement (ADR-0028).
    """
    audit(spec)
    response.delete_cookie(
        key=spec.name,
        path=spec.path,
        httponly=spec.http_only,
        secure=spec.secure,
        samesite="strict",
    )


def session_spec(name: str, *, max_age: int, secure: bool) -> CookieSpec:
    """Build a session-cookie spec, auditing the attributes actually used.

    Args:
        name: `SESSION_COOKIE` or `OPS_SESSION_COOKIE`.
        max_age: Seconds, from the session policy.
        secure: From `Settings.cookie_secure`. False only on local http, where
            the browser rejects the `Secure` attribute — the sole
            environment-conditional value, and it concerns transport, not
            product behaviour.

    Raises:
        CookiePolicyViolation: If the resulting cookie would violate SEC-002.
            On a `__Host-` name with `secure=False` this fires at construction —
            which is the correct outcome: that combination cannot work, and
            failing loudly beats a browser silently dropping every session.
    """
    return CookieSpec(name=name, max_age=max_age, secure=secure)


def audit_all(specs: tuple[CookieSpec, ...]) -> None:
    """Audit every cookie the application can set. Called from startup."""
    for spec in specs:
        audit(spec)
