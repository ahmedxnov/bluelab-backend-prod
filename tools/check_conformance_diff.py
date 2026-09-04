#!/usr/bin/env python3
"""Regenerate the TypeScript client and the problem-type registry from
`api/openapi.yaml`, diff against what the server serves, fail on any drift.

Build-failing **before the first feature merge** (api gate C-2, ADR-0035).

    python tools/check_conformance_diff.py                     # commit gate stage 3
    python tools/check_conformance_diff.py --update-baseline   # after closing gaps

## Direction of authority

ADR-0035 decision 3 fixes it: **code → contract**. `api/openapi.yaml` is authored
and reviewed; FastAPI's generated schema is an *output* that must match. A shape
the implementation genuinely needs changed routes back through the Phase 5 gate as
an amendment (api/04 §5) — the contract moves first, then code. So every finding
here is phrased as "the server disagrees with the contract", never the reverse.

## Two kinds of finding, and only one of them is forgivable

**Contradiction** — the server serves a path the contract does not define, an
operationId that differs, a problem type the catalog never specified, or a status
code that disagrees with it. Code and specification actively disagree. Never
baselined, never forgiven; this is the drift C-2 exists to stop.

**Gap** — the contract defines an operation nobody has implemented yet, or the
catalog specifies a problem type the registry does not carry. That is absence, not
disagreement, and on a part-built product it is the normal state. Gaps ride the
ratchet in `_ratchet.py`: today's are recorded, new ones fail, closed ones must be
struck from the file.

That split is what lets this gate be wired in *before* the first feature merge, as
C-2 requires, rather than after the surface is complete.

## What is compared

1. **The problem-type registry** — `platform/errors/catalog.py` against the
   registry table in `api/03-error-catalog.api.md`. Both exist today, and the
   client branches on `type` alone (ux/05 §3), so a slug is a contract element and
   a status is part of it.
2. **The operation set** — `api/openapi.yaml` against the FastAPI app's generated
   schema, keyed on `(METHOD, path)` with the operationId checked alongside.
3. **The TypeScript client** — generated from the contract in CI, never hand-written
   (ADR-0013). Reported as not-run until the frontend toolchain is installed; see
   `_typescript_client`. It is reported rather than skipped silently, because a
   check that quietly stops checking is the failure mode this whole file exists to
   prevent.

## Exit codes

* **0** — no contradictions, and the ratchet is satisfied.
* **1** — drift. The gate is red (pipeline/02 §2 row 3).
* **2** — the check could not run: the contract is unreadable, or the app module
  exists but could not be imported. Never 0, because a conformance gate that
  reports success without having compared anything is worse than none.
"""

from __future__ import annotations

import argparse
import importlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
BUILD_ROOT = REPO_ROOT.parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _ratchet

CONTRACT = BUILD_ROOT / "api" / "openapi.yaml"
ERROR_CATALOG = BUILD_ROOT / "api" / "03-error-catalog.api.md"
FRONTEND = BUILD_ROOT / "Implementation" / "bluelab-frontend"

APP_MODULE = "bluelab.entrypoints.api"
APP_FACTORY = "create_app"
APP_ATTR = "app"
"""The factory is tried first, then a module-level instance.

`entrypoints.api` deliberately exposes no module-level `app` — building one at
import time would resolve `Settings` on import and make the module unimportable
without the full environment. Looking only for `app` is how this check quietly
went back to reporting "the application plane is still scaffold" after the
factory landed, while the server was in fact serving three operations."""

BASELINE = "check-conformance-diff"

HTTP_METHODS = frozenset(
    {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
)

_CATALOG_ROW = re.compile(r"^\|\s*`([a-z0-9-]+)`\s*\|\s*(\d{3})\s*\|", re.MULTILINE)
"""A registry row in `api/03`: `| \\`slug\\` | 422 | when | retry |`.

Anchored on the status column so the tables that are *not* registries — §1's shape
description, §3's discipline notes — cannot contribute rows. `_check_problems`
asserts the harvest is plausible rather than trusting that silently.
"""

MIN_CATALOG_ROWS = 50
"""api/03 §2 carries the whole 4xx/5xx surface across seven subsections. If the
parse returns far fewer, the document's table shape has moved and this check has
stopped checking — which must fail loudly rather than report a clean diff."""


# ── findings ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Finding:
    kind: str
    subject: str
    detail: str

    def render(self) -> str:
        lines = [f"  [{self.kind}] {self.subject}"]
        lines.extend(f"      {line}" for line in self.detail.splitlines())
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class Result:
    contradictions: list[Finding]
    gaps: set[str]
    notes: list[str]


# ── 1. the problem-type registry ──────────────────────────────────────────────


def documented_problems() -> dict[str, int]:
    """Slug → status, harvested from the `api/03` registry tables.

    Returns:
        Every documented problem slug mapped to its specified HTTP status.

    Raises:
        ValueError: If fewer than `MIN_CATALOG_ROWS` rows parse, which means the
            document's table shape moved and the diff would otherwise compare
            against almost nothing and call it clean.
        OSError: If the catalog is unreadable.
    """
    text = ERROR_CATALOG.read_text(encoding="utf-8")
    rows = {slug: int(status) for slug, status in _CATALOG_ROW.findall(text)}
    if len(rows) < MIN_CATALOG_ROWS:
        raise ValueError(
            f"{ERROR_CATALOG.name}: parsed only {len(rows)} registry rows, expected "
            f"at least {MIN_CATALOG_ROWS}. The document's table shape has changed and "
            f"this check would otherwise report a clean diff against almost nothing."
        )
    return rows


def check_problems() -> Result:
    """The registry against the catalog it mirrors.

    `platform/errors/catalog.py` is data rather than exception classes precisely so
    this comparison is possible — its own docstring says so. The set of problems
    the server can produce has to be enumerable, or it is discoverable by grep and
    by nothing else.
    """
    from bluelab.platform.errors.catalog import REGISTRY

    documented = documented_problems()
    registered = {problem.slug: problem.status for problem in REGISTRY.all()}

    contradictions: list[Finding] = []
    gaps: set[str] = set()

    for slug in sorted(set(registered) - set(documented)):
        contradictions.append(
            Finding(
                "problem-uncontracted",
                slug,
                f"the registry can produce {slug} ({registered[slug]}) but "
                f"api/03 does not specify it. A client branches on `type` alone "
                f"(ux/05 §3), so a problem nobody documented is one no client "
                f"knows how to render.",
            )
        )

    for slug in sorted(set(documented) & set(registered)):
        if documented[slug] != registered[slug]:
            contradictions.append(
                Finding(
                    "problem-status",
                    slug,
                    f"api/03 specifies {documented[slug]}, the registry returns "
                    f"{registered[slug]}. Changing a problem's status code is "
                    f"breaking under api/04 §3 — it does not belong in v1 at all.",
                )
            )

    for slug in sorted(set(documented) - set(registered)):
        gaps.add(f"problem:{slug}")

    return Result(contradictions, gaps, [])


# ── 2. the operation set ──────────────────────────────────────────────────────


def contract_operations() -> dict[tuple[str, str], str]:
    """`(METHOD, path)` → operationId, from the authored contract.

    Returns:
        Every operation the contract defines, keyed by method and path.

    Raises:
        ValueError: If the contract yields no operations at all.
        OSError: If `api/openapi.yaml` is unreadable.
        yaml.YAMLError: If it is not parseable YAML.
    """
    spec = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    operations: dict[tuple[str, str], str] = {}
    for path, item in (spec.get("paths") or {}).items():
        for method, operation in item.items():
            if method.lower() in HTTP_METHODS:
                operations[(method.upper(), path)] = operation.get("operationId", "")
    if not operations:
        raise ValueError(f"{CONTRACT.name}: no operations found under `paths`")
    return operations


def served_operations() -> tuple[dict[tuple[str, str], str] | None, str]:
    """What the app actually serves, or `None` with the reason it served nothing.

    The distinction matters more than it looks. "There is no app yet" is the honest
    current state and every contracted operation is a gap. "The app exists but its
    import blew up" must *not* collapse to the same answer — that would report zero
    served operations, match the baseline, and pass. A conformance gate that goes
    green because it could not load the server is the exact false green this file
    is supposed to make impossible, so that case raises.

    Returns:
        `(operations, "")` where operations maps `(METHOD, path)` to operationId,
        or `(None, reason)` when the application plane is still scaffold.

    Raises:
        ValueError: If the app module exists but cannot be imported.
    """
    try:
        module = importlib.import_module(APP_MODULE)
    except Exception as exc:
        raise ValueError(
            f"{APP_MODULE} exists but could not be imported: "
            f"{type(exc).__name__}: {exc}\n"
            f"Refusing to treat that as 'serves nothing' — it would pass this gate."
        ) from exc

    factory = getattr(module, APP_FACTORY, None)
    app = getattr(module, APP_ATTR, None)
    if factory is None and app is None:
        return None, (
            f"{APP_MODULE} exposes neither {APP_FACTORY}() nor {APP_ATTR} — the "
            f"application plane is still scaffold, so every contracted operation "
            f"is an open gap"
        )

    if factory is not None:
        try:
            app = factory()
        except Exception as exc:
            raise ValueError(
                f"{APP_MODULE}:{APP_FACTORY}() raised "
                f"{type(exc).__name__}: {exc}\n"
                f"Refusing to treat that as 'serves nothing'. The factory resolves "
                f"Settings, so this is usually a missing environment variable — "
                f"which must not read as an empty surface."
            ) from exc

    schema = app.openapi()  # type: ignore[union-attr]
    operations: dict[tuple[str, str], str] = {}
    for path, item in (schema.get("paths") or {}).items():
        for method, operation in item.items():
            if method.lower() in HTTP_METHODS:
                operations[(method.upper(), path)] = operation.get("operationId", "")
    return operations, ""


def check_operations() -> Result:
    contracted = contract_operations()
    served, reason = served_operations()

    contradictions: list[Finding] = []
    gaps: set[str] = set()
    notes: list[str] = []

    if served is None:
        notes.append(reason)
        gaps.update(f"operation:{method} {path}" for method, path in contracted)
        return Result(contradictions, gaps, notes)

    for key in sorted(served.keys() - contracted.keys()):
        method, path = key
        contradictions.append(
            Finding(
                "operation-uncontracted",
                f"{method} {path}",
                "the server serves this; the contract does not define it. The "
                "contract moves first (ADR-0035 decision 3) — amend "
                "api/openapi.yaml through the Phase 5 gate, then implement.",
            )
        )

    for key in sorted(served.keys() & contracted.keys()):
        if served[key] != contracted[key]:
            method, path = key
            contradictions.append(
                Finding(
                    "operation-id",
                    f"{method} {path}",
                    f"contract operationId is {contracted[key]!r}, the server's is "
                    f"{served[key]!r}. The generated client is named from this, so "
                    f"a rename breaks every caller (ADR-0013).",
                )
            )

    for key in sorted(contracted.keys() - served.keys()):
        method, path = key
        gaps.add(f"operation:{method} {path}")

    notes.append(f"{len(served)} of {len(contracted)} contracted operations served")
    return Result(contradictions, gaps, notes)


# ── 3. the generated TypeScript client ────────────────────────────────────────


def _typescript_client() -> Result:
    """The client is generated from the contract in CI, never hand-written.

    Nothing here to diff *yet*: `bluelab-frontend` has no `node_modules`, so the
    generator cannot run, and `src/api/` holds docstring scaffold with no committed
    generated artifact to compare against. Once the frontend toolchain is installed
    this grows a real comparison — regenerate from `api/openapi.yaml` and diff
    against what is committed.

    Reported as not-run rather than skipped in silence. C-2 covers schema *and*
    client, so a summary that did not mention this would overstate what the gate
    verified — and quietly reduced scope is precisely how a green build stops
    meaning anything.
    """
    if (FRONTEND / "node_modules").is_dir():
        return Result(
            [],
            set(),
            ["TypeScript client: frontend toolchain present — regeneration diff not implemented"],
        )
    return Result(
        [],
        set(),
        [
            (
                "TypeScript client: NOT CHECKED — bluelab-frontend has no node_modules, "
                "so the generator cannot run. C-2 is not fully closed until it does."
            )
        ],
    )


# ── driver ────────────────────────────────────────────────────────────────────


def run() -> tuple[list[Finding], set[str], list[str]]:
    contradictions: list[Finding] = []
    gaps: set[str] = set()
    notes: list[str] = []
    for result in (check_problems(), check_operations(), _typescript_client()):
        contradictions.extend(result.contradictions)
        gaps.update(result.gaps)
        notes.extend(result.notes)
    return contradictions, gaps, notes


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diff the server and the generated client against api/openapi.yaml."
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="record today's gaps as accepted. Contradictions are never recorded.",
    )
    args = parser.parse_args()

    try:
        contradictions, gaps, notes = run()
    except (ValueError, OSError, yaml.YAMLError) as exc:
        # yaml.YAMLError belongs here and is easy to miss: a malformed contract is
        # the most likely way this check ever fails to run, and without it the tool
        # dies on a traceback whose exit code is 1 — indistinguishable from real
        # drift, and pointing the reader at the wrong problem.
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    for note in notes:
        print(f"  {note}")
    # Findings go to stderr and notes to stdout; unflushed, the log reads as though
    # the notes came after the failures they contextualise.
    sys.stdout.flush()

    if args.update_baseline:
        if contradictions:
            # Recording a baseline while the server contradicts the contract would
            # bake the disagreement in as "accepted". Gaps are forgivable; drift is
            # the thing this gate exists to refuse.
            print(
                f"\nERROR: {len(contradictions)} contradiction(s) — fix these before "
                f"baselining. A baseline forgives absence, never disagreement.",
                file=sys.stderr,
            )
            for finding in contradictions:
                print(finding.render(), end="\n\n", file=sys.stderr)
            return 1
        target = _ratchet.write(
            BASELINE,
            gaps,
            note=(
                "Contract elements with no implementation yet (api C-2, ADR-0035). "
                "This list may only shrink."
            ),
        )
        print(f"\nwrote {target.relative_to(REPO_ROOT).as_posix()} — {len(gaps)} open gap(s)")
        return 0

    verdict = _ratchet.evaluate(BASELINE, gaps)
    ratchet_lines = _ratchet.render(BASELINE, verdict, noun="unimplemented contract element(s)")

    if not contradictions and not ratchet_lines:
        print(f"\nconformant — {len(verdict.still_open)} known gap(s), no drift")
        return 0

    print(file=sys.stderr)
    if contradictions:
        print(
            f"DRIFT — {len(contradictions)} contradiction(s) between the server and "
            f"the contract\n",
            file=sys.stderr,
        )
        for finding in contradictions:
            print(finding.render(), end="\n\n", file=sys.stderr)
    for line in ratchet_lines:
        print(f"  {line}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
