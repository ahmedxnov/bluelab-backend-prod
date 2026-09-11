#!/usr/bin/env python3
"""Parse every `FR-*`/`NFR-*`/`CMP-*`/`SEC-*` from `specs/01`, `specs/02`, and
`api/06`, and assert each has a tagged, non-skipped test.

A skipped or `xfail` placeholder counts as **uncovered** — a placeholder cannot
fail, and a test that cannot fail proves nothing. This is what makes the
traceability spine self-enforcing across future amendments rather than true only
on the day the phase closed (quality/08 §5, ADR-0062).

    python tools/check_requirement_coverage.py                    # commit gate stage 10
    python tools/check_requirement_coverage.py --update-baseline  # after adding tests
    python tools/check_requirement_coverage.py --list-uncovered   # what to write next

## Two findings, and only one is forgivable

**Contradiction** — a test tagged with an id no document declares. Never baselined.
It is worse than an untested requirement, because it *inflates* the number: the
tag reads as proof, the suite goes green, and the requirement it was meant to
cover is silently still bare. A typo in a `verifies` string is the traceability
spine quietly breaking.

**Gap** — a declared requirement with no live test. On a part-built product that is
the normal state, so gaps ride the ratchet in `_ratchet.py`: today's are recorded,
new ones fail, and closing one requires striking it from the file.

## Where requirements are declared, and why only these three files

Requirements are *cited* everywhere — every ADR, every design doc, most
docstrings. They are **declared** in exactly three places, and citing is not
declaring:

* `specs/01` — `### NFR-001`, `### CMP-004` headings
* `specs/02` — `### SEC-001` headings
* `api/06`  — the FR reachability matrix, one table row per FR

Harvesting citations instead would inflate the denominator with every passing
mention and make the ratio meaningless.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_ROOT = REPO_ROOT / "contracts" / "platform"
CONTRACT_LOCK = REPO_ROOT / "contracts" / "platform.lock.json"
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _ratchet

TESTS = REPO_ROOT / "tests"
BASELINE = "check-requirement-coverage"

SPEC_NFR_CMP = CONTRACT_ROOT / "specs" / "01-nfr-and-compliance.spec.md"
SPEC_SEC = CONTRACT_ROOT / "specs" / "02-security-requirements.spec.md"
API_FR_COVERAGE = CONTRACT_ROOT / "api" / "06-fr-coverage.api.md"

_HEADING = re.compile(r"^###\s+((?:NFR|CMP|SEC)-\d{3})\b", re.MULTILINE)
"""`### SEC-041: Launch data-layer network restriction` — the declaration form in
specs/01 and specs/02. The colon and title are optional; the id is not."""

_FR_ROW = re.compile(r"^\|\s*(FR-[A-Z]{3}-\d{3})\s*\|", re.MULTILINE)
"""A row of api/06's reachability matrix: `| FR-IDA-001 | realized by … |`."""

_AC_HEADING = re.compile(r"^###\s+(AC-[A-Z]{3}-\d{3})\b", re.MULTILINE)
"""`### AC-IDA-006: Cross-org denial` in the feature specs."""

SKIP_MARKS = frozenset({"skip", "skipif", "xfail"})
"""ADR-0062's rule, mechanised. `xfail` is included deliberately: until it starts
passing it asserts nothing, and a requirement whose only evidence is an expected
failure is uncovered."""

MIN_DECLARED = 150
"""184 today across the three files. A parse that collapses well below that means a
heading or table shape moved, and the check would otherwise report excellent
coverage of almost nothing."""


@dataclass(frozen=True, slots=True)
class Tag:
    """One `verifies` id, and where it was claimed."""

    requirement: str
    location: str


# ── what the documents declare ────────────────────────────────────────────────


def declared() -> dict[str, str]:
    """Requirement id → the document that declares it.

    Returns:
        Every `FR-*`/`NFR-*`/`CMP-*`/`SEC-*` declared across the three documents,
        mapped to the filename that declares it.

    Raises:
        ValueError: If fewer than `MIN_DECLARED` parse, which means a heading or
            table shape moved and the check would report near-total coverage of a
            near-empty set.
        OSError: If any of the three documents is unreadable.
    """
    out: dict[str, str] = {}
    for path, pattern in (
        (SPEC_NFR_CMP, _HEADING),
        (SPEC_SEC, _HEADING),
        (API_FR_COVERAGE, _FR_ROW),
    ):
        for requirement in pattern.findall(path.read_text(encoding="utf-8")):
            out.setdefault(requirement, path.name)
    if len(out) < MIN_DECLARED:
        raise ValueError(
            f"parsed only {len(out)} requirement declarations, expected at least "
            f"{MIN_DECLARED}. A heading or table shape has changed, and this check "
            f"would otherwise report near-total coverage of a near-empty set."
        )
    return out


def citable() -> frozenset[str]:
    """Every identifier a `verifies` tag may legitimately name.

    Wider than the coverage denominator, and deliberately so. Tests cite
    acceptance criteria (`AC-IDA-006`) and decisions (`ADR-0023`) alongside the
    requirement families, and those are real identifiers declared elsewhere in the
    document set — an `AC-*` in a feature spec's `### AC-IDA-006:` heading, an
    `ADR-*` as a numbered file under any `adr/` directory.

    They do not enter the coverage ratio: quality/08 §5 and ADR-0062 track the four
    requirement families, and an acceptance criterion rolls up to the FR that owns
    it. But they must still be *checked to exist*, because a typo in any tag is the
    same defect — evidence pointing at nothing.
    """
    known = set(declared())
    for path in sorted((CONTRACT_ROOT / "specs").glob("*.spec.md")):
        known.update(_AC_HEADING.findall(path.read_text(encoding="utf-8")))
    lock = json.loads(CONTRACT_LOCK.read_text(encoding="utf-8"))
    adr_ids = lock.get("citable_adr_ids")
    if not isinstance(adr_ids, list) or not all(isinstance(item, str) for item in adr_ids):
        raise ValueError("platform.lock.json: citable_adr_ids must be a string list")
    known.update(adr_ids)
    return frozenset(known)


# ── what the tests claim ──────────────────────────────────────────────────────


def _mark_name(node: ast.expr) -> str | None:
    """The trailing attribute of a `pytest.mark.X` or `pytest.mark.X(...)` node."""
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _verifies_ids(node: ast.expr) -> list[str]:
    """The string arguments of a `@pytest.mark.verifies(...)` decorator."""
    if not isinstance(node, ast.Call) or _mark_name(node) != "verifies":
        return []
    return [
        arg.value
        for arg in node.args
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
    ]


def _module_marks(tree: ast.Module) -> list[ast.expr]:
    """`pytestmark = [...]` applies to every test in the file.

    Worth reading rather than assuming absent: a module-level skip silences a whole
    file's worth of claimed coverage at once, which is exactly the shape of an
    accidentally-quarantined suite still reporting its requirements as covered.
    """
    out: list[ast.expr] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets):
            continue
        value = node.value
        out.extend(value.elts if isinstance(value, (ast.List, ast.Tuple)) else [value])
    return out


def claimed() -> tuple[list[Tag], list[Tag]]:
    """Every `verifies` tag in the test tree, split into `(live, inert)`.

    Returns:
        `(live, inert)` — tags on tests that can fail, and tags on tests silenced
        by a `skip`/`skipif`/`xfail` mark at either function or module level. Only
        the first confers coverage (ADR-0062).

    Raises:
        SyntaxError: If a test file does not parse.
        OSError: If a test file is unreadable.
    """
    live: list[Tag] = []
    inert: list[Tag] = []

    for path in sorted(TESTS.rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        module_skipped = any(_mark_name(m) in SKIP_MARKS for m in _module_marks(tree))

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test_"):
                continue

            ids: list[str] = []
            for decorator in node.decorator_list:
                ids.extend(_verifies_ids(decorator))
            if not ids:
                continue

            skipped = module_skipped or any(
                _mark_name(d) in SKIP_MARKS for d in node.decorator_list
            )
            location = f"{path.relative_to(REPO_ROOT).as_posix()}::{node.name}"
            (inert if skipped else live).extend(
                Tag(requirement=r, location=location) for r in ids
            )

    return live, inert


# ── the check ─────────────────────────────────────────────────────────────────


def run() -> tuple[list[str], set[str], list[str]]:
    """Returns `(contradictions, gaps, notes)`."""
    requirements = declared()
    universe = citable()
    live, inert = claimed()
    covered = {tag.requirement for tag in live}

    unknown: dict[str, list[str]] = {}
    for tag in live + inert:
        if tag.requirement not in universe:
            unknown.setdefault(tag.requirement, []).append(tag.location)

    contradictions = [
        f"{requirement} — tagged by a test but declared by no document.\n"
        f"A mistyped id inflates coverage: the tag reads as proof, the suite goes "
        f"green, and the requirement it meant to cover is still bare.\n  "
        + "\n  ".join(sorted(unknown[requirement]))
        for requirement in sorted(unknown)
    ]

    gaps = {r for r in requirements if r not in covered}

    # Intersected, not raw. `covered` holds every live tag id, and tests legitimately
    # cite AC and ADR identifiers alongside requirements — counting those in the
    # numerator would report coverage the denominator never contained.
    covered_requirements = covered & set(requirements)

    inert_only = sorted({t.requirement for t in inert if t.requirement in requirements} - covered)
    notes = [
        f"{len(covered_requirements)} of {len(requirements)} requirements covered by a live test"
    ]
    if inert_only:
        notes.append(
            f"{len(inert_only)} requirement(s) claimed ONLY by a skipped or xfail "
            f"test — uncovered under ADR-0062: {', '.join(inert_only[:8])}"
            + (" …" if len(inert_only) > 8 else "")
        )
    return contradictions, gaps, notes


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Assert every declared requirement has a tagged, non-skipped test."
    )
    parser.add_argument("--update-baseline", action="store_true", help="record today's gaps as accepted")
    parser.add_argument(
        "--list-uncovered", action="store_true", help="print every uncovered requirement, then exit 0"
    )
    args = parser.parse_args()

    try:
        contradictions, gaps, notes = run()
    except (ValueError, OSError, SyntaxError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.list_uncovered:
        for requirement in sorted(gaps):
            print(requirement)
        return 0

    for note in notes:
        print(f"  {note}")
    sys.stdout.flush()

    if args.update_baseline:
        if contradictions:
            print(
                f"\nERROR: {len(contradictions)} tag(s) name a requirement no document "
                f"declares. Fix these first — a baseline forgives an untested "
                f"requirement, never a lying tag.",
                file=sys.stderr,
            )
            for line in contradictions:
                print(f"  [tag-unknown] {line}\n", file=sys.stderr)
            return 1
        target = _ratchet.write(
            BASELINE,
            gaps,
            note=(
                "Requirements with no live test yet (quality/08 §5, ADR-0062). "
                "This list may only shrink."
            ),
        )
        print(f"\nwrote {target.relative_to(REPO_ROOT).as_posix()} — {len(gaps)} uncovered")
        return 0

    verdict = _ratchet.evaluate(BASELINE, gaps)
    ratchet_lines = _ratchet.render(BASELINE, verdict, noun="uncovered requirement(s)")

    if not contradictions and not ratchet_lines:
        print(f"\ncoverage holds — {len(verdict.still_open)} known gap(s), no untraceable tags")
        return 0

    print(file=sys.stderr)
    for line in contradictions:
        print(f"  [tag-unknown] {line}\n", file=sys.stderr)
    for line in ratchet_lines:
        print(f"  {line}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
