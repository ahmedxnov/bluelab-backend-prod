#!/usr/bin/env python3
"""Startup and CI audit of every cookie the application sets (SEC-002).

    python tools/audit_cookie_attributes.py     # commit gate stage 9

This is the **CI half**. `platform/security/cookies.py` owns the startup half, and
its docstring explains why both exist: the commit-gate scan reads the source, the
startup audit reads the constructed cookie, and they catch different mistakes. A
value assembled at runtime from configuration passes a source scan and can still
be wrong; a cookie set from a route that never went through the helper is
invisible to the startup audit because it never reaches it.

## The property this scan actually enforces

Not "every cookie has the right attributes" — a source scan cannot know that.
What it enforces is the **chokepoint**: `set_cookie` and `delete_cookie` are
called from `platform/security/cookies.py` and nowhere else. That single fact is
what makes the startup audit total, because every cookie the application can emit
has then passed through `audit()`.

A route that calls `response.set_cookie(...)` directly is the failure this exists
to catch, and it is an easy one to write — FastAPI puts the method on every
`Response` object, one autocomplete away.

## Then the attributes at the chokepoint itself

Inside `cookies.py` the emitted attributes are checked against SEC-002 directly:
`SameSite=Strict`, `Path=/`, `HttpOnly`, and **no `Domain`** — the one the
`__Host-` prefix forbids and the reason the prefix is used at all. The names are
checked for the prefix too, because `__Host-` is what makes the browser enforce
the rest rather than us merely asserting it.

## Exit codes

* **0** — the chokepoint holds and every attribute conforms.
* **1** — a violation. The gate is red.
* **2** — the scan could not run (unparseable source, `cookies.py` absent).
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BUILD_ROOT = REPO_ROOT.parents[1]

BACKEND_SRC = REPO_ROOT / "src"
COOKIE_MODULE = BACKEND_SRC / "bluelab" / "platform" / "security" / "cookies.py"

COOKIE_SETTERS = frozenset({"set_cookie", "delete_cookie"})
"""Starlette's `Response` methods. Anything that emits a `Set-Cookie` header goes
through one of these."""

REQUIRED_PREFIX = "__Host-"
REQUIRED_SAME_SITE = "strict"
REQUIRED_PATH = "/"

NAME_CONSTANTS = ("SESSION_COOKIE", "OPS_SESSION_COOKIE")
"""Every cookie name the application knows. Both must carry the prefix; the ops
session is a separate cookie for a separate surface (ADR-0010), not a customer
session with extra rights, and it gets the same treatment."""


@dataclass(frozen=True, slots=True)
class Violation:
    kind: str
    location: str
    detail: str

    def render(self) -> str:
        lines = [f"  [{self.kind}] {self.location}"]
        lines.extend(f"      {line}" for line in self.detail.splitlines())
        return "\n".join(lines)


def _rel(path: Path) -> str:
    """A repo-relative label, falling back to the absolute path.

    `relative_to` raises for anything outside the tree, and a crash while
    formatting a violation would surface as "the scan could not run" — strictly
    worse than the violation it was about to report.
    """
    try:
        return path.relative_to(BUILD_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _label(path: Path, node: ast.AST) -> str:
    return f"{_rel(path)}:{getattr(node, 'lineno', 0)}"


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _cookie_calls(tree: ast.Module) -> Iterator[ast.Call]:
    """Every `response.set_cookie(...)` / `.delete_cookie(...)` in one module.

    Shared by both checks so that "what counts as emitting a cookie" is decided in
    exactly one place. Two copies of this predicate would be two chances for the
    chokepoint check and the attribute check to disagree about what they are
    looking at — and the one that looked at less would be the one still passing.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in COOKIE_SETTERS:
            yield node


def check_chokepoint() -> list[Violation]:
    """`set_cookie` / `delete_cookie` may be called from `cookies.py` alone."""
    violations: list[Violation] = []
    for path in sorted(BACKEND_SRC.rglob("*.py")):
        if path == COOKIE_MODULE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for call in _cookie_calls(tree):
            attr = call.func.attr if isinstance(call.func, ast.Attribute) else "set_cookie"
            violations.append(
                Violation(
                    "cookie-bypass",
                    _label(path, call),
                    f"calls {attr}() outside {_rel(COOKIE_MODULE)}. Every cookie must "
                    f"go through set_session_cookie/clear_session_cookie so audit() "
                    f"sees it — otherwise the startup audit is not total and SEC-002 "
                    f"holds only for the cookies someone remembered to route through "
                    f"it.",
                )
            )
    return violations


# ── one SEC-002 attribute rule per function ──────────────────────────────────
#
# Each takes the emitting call and returns a violation or nothing. Split apart so
# a reader can check one rule of SEC-002 at a time against the requirement text,
# and so adding the next attribute means adding a function rather than growing a
# branch inside a loop.


def _rule_no_domain(call: ast.Call, location: str) -> Violation | None:
    if _keyword(call, "domain") is None:
        return None
    return Violation(
        "cookie-domain",
        location,
        "passes `domain`. The __Host- prefix forbids it, and it is the attribute "
        "that would let a hostile or compromised subdomain set a cookie this "
        "application would accept.",
    )


def _rule_same_site_strict(call: ast.Call, location: str) -> Violation | None:
    same_site = _keyword(call, "samesite")
    if isinstance(same_site, ast.Constant) and same_site.value == REQUIRED_SAME_SITE:
        return None
    rendered = ast.unparse(same_site) if same_site is not None else "absent"
    return Violation(
        "cookie-samesite",
        location,
        f"samesite is {rendered}, must be the literal {REQUIRED_SAME_SITE!r}. "
        f"SameSite=Strict is the reason this application carries no CSRF tokens "
        f"(api/00 §3) — weakening it silently removes the control it stands in for.",
    )


def _rule_http_only(call: ast.Call, location: str) -> Violation | None:
    if _keyword(call, "httponly") is not None:
        return None
    return Violation(
        "cookie-httponly",
        location,
        "does not pass `httponly`. Starlette defaults it to False, so an omission "
        "is an opt-out: the session becomes readable by any script that runs on "
        "the page.",
    )


def _rule_path_root(call: ast.Call, location: str) -> Violation | None:
    if _keyword(call, "path") is not None:
        return None
    return Violation(
        "cookie-path",
        location,
        f"does not pass `path`; the __Host- prefix requires {REQUIRED_PATH!r}.",
    )


AttributeRule = Callable[[ast.Call, str], Violation | None]

ATTRIBUTE_RULES: tuple[AttributeRule, ...] = (
    _rule_no_domain,
    _rule_same_site_strict,
    _rule_http_only,
    _rule_path_root,
)
"""SEC-002, one entry per clause. Every rule runs against every emitting call —
none short-circuits, because a cookie can breach more than one at a time and a
report naming only the first would send someone round twice."""


def check_attributes() -> list[Violation]:
    """The attributes actually emitted at the chokepoint.

    Returns:
        One violation per SEC-002 deviation, plus a `cookie-none` violation if the
        module emits no cookie at all — an audit with nothing to audit must not
        report success.

    Raises:
        OSError: If the cookie module is absent.
        SyntaxError: If it does not parse.
    """
    if not COOKIE_MODULE.exists():
        raise OSError(f"{COOKIE_MODULE} does not exist — nothing to audit")

    tree = ast.parse(COOKIE_MODULE.read_text(encoding="utf-8"), filename=str(COOKIE_MODULE))
    violations: list[Violation] = []
    calls = 0

    for call in _cookie_calls(tree):
        calls += 1
        location = _label(COOKIE_MODULE, call)
        for rule in ATTRIBUTE_RULES:
            violation = rule(call, location)
            if violation is not None:
                violations.append(violation)

    if not calls:
        # An audit that found nothing to audit must not report success. Either the
        # helper was renamed or the module stopped emitting cookies, and both mean
        # this check has quietly stopped checking.
        violations.append(
            Violation(
                "cookie-none",
                _rel(COOKIE_MODULE),
                "no set_cookie/delete_cookie call found in the one module allowed to "
                "make them. This scan has nothing to verify, which is not the same "
                "as everything being correct.",
            )
        )

    return violations


def _string_constants(tree: ast.Module) -> dict[str, str]:
    """Every module-level `NAME = "value"` / `NAME: Final = "value"`.

    Harvesting only. Judging what the values *should* be belongs to the caller —
    keeping the two apart is what lets the AST handling be read once and the
    SEC-002 rules be read on their own.
    """
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        target: str | None = None
        assigned: ast.expr | None = None
        if isinstance(node, ast.AnnAssign):
            # `SESSION_COOKIE: Final = "…"`. A bare annotation carries no value at
            # all, which is why this is Optional rather than assumed present.
            if isinstance(node.target, ast.Name):
                target, assigned = node.target.id, node.value
        elif isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if names:
                target, assigned = names[0], node.value
        if target is None or not isinstance(assigned, ast.Constant):
            continue
        if isinstance(assigned.value, str):
            found[target] = assigned.value
    return found


def _check_prefix(constant: str, value: str | None) -> Violation | None:
    if value is None:
        return Violation(
            "cookie-name-missing",
            _rel(COOKIE_MODULE),
            f"{constant} is not defined as a string constant — this scan cannot "
            f"confirm its prefix.",
        )
    if value.startswith(REQUIRED_PREFIX):
        return None
    return Violation(
        "cookie-prefix",
        _rel(COOKIE_MODULE),
        f"{constant} is {value!r}; SEC-002 requires the {REQUIRED_PREFIX!r} prefix. "
        f"The prefix is what makes the browser enforce Secure, Path=/ and no Domain, "
        f"rather than us asserting them and hoping.",
    )


def _check_literal(constant: str, value: str | None, expected: str) -> Violation | None:
    # Absent is not a violation here: these two constants are conveniences, and
    # `ATTRIBUTE_RULES` already checks what the emitting call actually passes.
    if value is None or value == expected:
        return None
    return Violation(
        "cookie-constant",
        _rel(COOKIE_MODULE),
        f"{constant} is {value!r}, expected {expected!r}.",
    )


def check_names() -> list[Violation]:
    """Every cookie name constant carries the `__Host-` prefix.

    Returns:
        One violation per non-conforming constant.

    Raises:
        OSError: If the cookie module is unreadable.
        SyntaxError: If it does not parse.
    """
    constants = _string_constants(
        ast.parse(COOKIE_MODULE.read_text(encoding="utf-8"), filename=str(COOKIE_MODULE))
    )

    candidates = [_check_prefix(name, constants.get(name)) for name in NAME_CONSTANTS]
    candidates += [
        _check_literal(name, constants.get(name), expected)
        for name, expected in (("SAME_SITE", REQUIRED_SAME_SITE), ("COOKIE_PATH", REQUIRED_PATH))
    ]
    return [violation for violation in candidates if violation is not None]


def main() -> int:
    argparse.ArgumentParser(
        description="Audit every cookie the application sets (SEC-002)."
    ).parse_args()

    try:
        violations = check_chokepoint() + check_names() + check_attributes()
    except (OSError, SyntaxError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if not violations:
        print(
            f"cookies conform — {REQUIRED_PREFIX} prefix, HttpOnly, Secure, "
            f"SameSite={REQUIRED_SAME_SITE.title()}, Path={REQUIRED_PATH}, no Domain"
        )
        return 0

    print(f"COOKIE POLICY — {len(violations)} violation(s)\n", file=sys.stderr)
    for violation in violations:
        print(violation.render(), end="\n\n", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
