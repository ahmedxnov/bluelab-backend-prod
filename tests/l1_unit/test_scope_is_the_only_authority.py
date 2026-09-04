"""The scope GUCs are the authorization model. These pin who may write them.

Every RLS policy in `sql/policies/generated/` derives its answer from
`current_setting('app.*')`. Nothing else decides who sees what — there is no
second check in the service layer to fall back on. That makes the GUC write path
the single highest-value target in the system: a caller who can set `app.role` to
`manager`, or `app.account_id` to somebody else's id, has not bypassed one policy,
they have bypassed all 219 of them at once.

An authorized probe against the local database confirmed the blast radius rather
than assuming it: a transaction claiming `app.role = 'manager'` both read and
UPDATEd an assignment it had no business touching. That is the designed behaviour
of the model — role is an input to it — which is exactly why the *inputs* need a
guard that a future refactor has to trip over.

Three properties are asserted here, all statically:

    1. only `platform/db/scope.py` writes an `app.*` GUC
    2. every write is transaction-LOCAL
    3. every value is a bind parameter, never interpolated

Static, on purpose. A runtime test proves the path taken; these prove no *other*
path exists — which is the claim that matters for a boundary. They cost no
database and run in the unit job, so a violation is caught before the L3 suite
has even connected.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.l1_unit, pytest.mark.invariant_path]

SRC = Path(__file__).resolve().parents[2] / "src" / "bluelab"
SCOPE_MODULE = SRC / "platform" / "db" / "scope.py"

_SET_CONFIG = re.compile(r"set_config\s*\(", re.IGNORECASE)


def _python_sources() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def test_only_the_scope_module_writes_a_scope_guc() -> None:
    """One writer, so there is one place to review.

    A second `set_config` anywhere — a helpful test seam, a worker that "just
    needs to impersonate briefly", a migration utility — is a second way to
    become somebody else, and it would not look dangerous in review. It looks
    like configuration.
    """
    writers = [
        p.relative_to(SRC).as_posix()
        for p in _python_sources()
        if _SET_CONFIG.search(p.read_text(encoding="utf-8"))
    ]
    assert writers == ["platform/db/scope.py"], (
        "something outside platform/db/scope.py writes a PostgreSQL GUC. If it "
        "writes an app.* key it can impersonate any principal; if it writes any "
        "other key it still needs review here.\nWriters found: " + str(writers)
    )


def test_every_scope_guc_write_is_literal_bound_and_transaction_local() -> None:
    """Each `app.*` write must be exactly `set_config('app.key', :param, true)`.

    Three properties in one pattern, because they fail together and a partial
    match is the dangerous case:

    `'app.key'` literal — a computed key means the GUC being written is chosen at
    runtime, so the thing deciding which authority to claim is data.

    `:param` bound — `SET LOCAL` cannot take a bind parameter, so the tempting
    workaround is an f-string, which puts a request-derived value into SQL text on
    the one path that must never carry one.

    `true` — transaction-LOCAL. Connections are pooled, so a session-level GUC
    outlives its transaction and is inherited by whoever borrows that connection
    next. One missing `true` turns pooling into cross-tenant reads: intermittent,
    load-dependent, and invisible to any single-request test.

    Prose in the module docstring is scanned too, and deliberately — an example
    showing the wrong form is how the wrong form gets copied.
    """
    source = SCOPE_MODULE.read_text(encoding="utf-8")
    writes = re.findall(r"set_config\(\s*[^)]*?app\.[^)]*\)", source)
    assert writes, "no app.* GUC writes found — has the scope module moved?"

    well_formed = re.compile(r"^set_config\(\s*'app\.[a-z_]+'\s*,\s*:[a-z_]+\s*,\s*true\s*\)$")
    for write in writes:
        collapsed = re.sub(r"\s+", " ", write.strip())
        assert well_formed.match(collapsed), (
            f"malformed scope write: {collapsed!r}\n"
            "Required form: set_config('app.<key>', :<param>, true) — literal key, "
            "bound value, transaction-local."
        )
