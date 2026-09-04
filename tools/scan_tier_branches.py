#!/usr/bin/env python3
"""Fail on any application branch keyed on the rollout tier (SEC-026, infra C-6,
specs/01 §3.2). The tier lives in composition and configuration; the same suite
must pass against every tier's build.

    python tools/scan_tier_branches.py          # commit gate stage 9

## Why a *branch*, and not a mention

SEC-026 is precise: "No application-code **branch** shall key on the rollout tier;
the tier shall live only in infrastructure composition and configuration values."
Naming the tier is fine and unavoidable — `telemetry/correlation.py` carries it as
a metric label, which is a configuration value doing exactly what the requirement
permits. Branching on it is the defect, because it means the T3 path carries code
the T1 demo never executed, and a security control that behaves differently per
rung is one nobody has tested at the rung that matters.

So this scan reads the **syntax tree**, not the text. A docstring that discusses
tiers at length cannot trip it; an `if settings.tier == "t1":` cannot hide from it.

## The word is overloaded, and getting that wrong would be worse than not scanning

`tier` in this product means two unrelated things:

* the **rollout tier** — T1/T2/T3 deployment rungs, which is what SEC-026 governs
* the **performance tier** — a rep's score band (`v_rep_month_tier`, V-4), which is
  core product logic that *must* branch

A scan that flagged the second would be turned off within a week, taking the first
with it. `PRODUCT_TIER_NAMES` carries the sanctioned product vocabulary; anything
else tested in a branch is reported for a human to adjudicate.

## Three checks

1. **Structural** — `Settings` carries no field whose name mentions a tier. This is
   the enforcement `platform/config.py` documents: code cannot branch on a value it
   cannot read.
2. **Branches** — no `if` / `match` / conditional expression tests a tier-ish name.
3. **Literals** — no comparison against a rollout-tier literal (`"tier 1"`, `"T3"`,
   `TIER_2`), whatever the variable is called. This is the one that catches a tier
   smuggled in under an innocent name.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BUILD_ROOT = REPO_ROOT.parents[1]

BACKEND_SRC = REPO_ROOT / "src"
FRONTEND_SRC = BUILD_ROOT / "Implementation" / "bluelab-frontend" / "src"
CONFIG = BACKEND_SRC / "bluelab" / "platform" / "config.py"

PRODUCT_TIER_NAMES = frozenset(
    {
        "performance_tier",
        "perf_tier",
        "rep_month_tier",
        "month_tier",
        "tier_band",
        "score_tier",
        "v_rep_month_tier",
    }
)
"""The rep score band (V-4, FR-SCR-005) — product logic that must branch.

Deliberately an explicit list rather than a pattern. A pattern wide enough to
catch these would be wide enough to let a rollout-tier branch through under a
similar name, and this is the one place where a false negative is expensive.
"""

_TIER_LITERAL = re.compile(r"^(tier[\s_-]?[123]|t[123])$", re.IGNORECASE)
"""`"tier 1"`, `"tier_2"`, `"T3"` — a rollout rung named as a value."""

_TIER_TOKEN = re.compile(r"tier", re.IGNORECASE)

_TS_CONDITIONAL = re.compile(r"\b(if|switch)\s*\(|\?[^:]*:|&&|\|\|")
"""Line-based, because there is no TypeScript parser here. Narrow on purpose: a
line must look like a conditional *and* mention a tier to be reported."""


@dataclass(frozen=True, slots=True)
class Branch:
    kind: str
    location: str
    detail: str

    def render(self) -> str:
        return f"  [{self.kind}] {self.location}\n      {self.detail}"


def _rel(path: Path) -> str:
    """A repo-relative label, falling back to the absolute path.

    `relative_to` raises for anything outside the tree, and a crash while
    formatting a finding would surface as "the scan could not run" — strictly
    worse than the finding it was about to report.
    """
    try:
        return path.relative_to(BUILD_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _names_in(node: ast.AST) -> list[str]:
    """Every identifier and attribute name appearing in an expression."""
    out: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            out.append(child.id)
        elif isinstance(child, ast.Attribute):
            out.append(child.attr)
    return out


def _tier_names(node: ast.AST) -> list[str]:
    return [
        name
        for name in _names_in(node)
        if _TIER_TOKEN.search(name) and name.lower() not in PRODUCT_TIER_NAMES
    ]


def _tier_literals(node: ast.AST) -> list[str]:
    return [
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant)
        and isinstance(child.value, str)
        and _TIER_LITERAL.match(child.value.strip())
    ]


def scan_python(root: Path) -> list[Branch]:
    """Walk every `.py` under `root` for branches keyed on the rollout tier.

    Args:
        root: Directory to scan recursively.

    Returns:
        One finding per tier-keyed name and per rollout-rung literal appearing in
        the *test* of a conditional. Empty when the tree is clean.

    Raises:
        SyntaxError: If a file does not parse.
        OSError: If a file is unreadable.
    """
    findings: list[Branch] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        label = _rel(path)

        for node in ast.walk(tree):
            test: ast.expr | None = None
            line = 0
            if isinstance(node, (ast.If, ast.IfExp, ast.While)):
                test, line = node.test, node.lineno
            elif isinstance(node, ast.Match):
                test, line = node.subject, node.lineno
            if test is None:
                continue

            for name in _tier_names(test):
                findings.append(
                    Branch(
                        "tier-branch",
                        f"{label}:{line}",
                        f"branches on {name!r}. The tier belongs in composition and "
                        f"configuration, never in a code path (SEC-026) — otherwise "
                        f"the T3 path carries branches the T1 demo never ran.",
                    )
                )
            for literal in _tier_literals(test):
                findings.append(
                    Branch(
                        "tier-literal",
                        f"{label}:{line}",
                        f"compares against {literal!r}, a rollout rung named as a "
                        f"value. The variable's name does not matter; the comparison "
                        f"is the branch.",
                    )
                )
    return findings


def scan_settings() -> list[Branch]:
    """`Settings` must expose no tier at all — the structural half.

    `platform/config.py` states this as its own enforcement: "there is no `tier`
    field on `Settings`. Code cannot branch on a value it cannot read." A scan that
    only looked for branches would let the field reappear and wait for someone to
    use it.
    """
    tree = ast.parse(CONFIG.read_text(encoding="utf-8"), filename=str(CONFIG))
    findings: list[Branch] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "Settings":
            continue
        for statement in node.body:
            target = None
            if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                target = statement.target.id
            elif isinstance(statement, ast.Assign):
                names = [t.id for t in statement.targets if isinstance(t, ast.Name)]
                target = names[0] if names else None
            if target and _TIER_TOKEN.search(target) and target.lower() not in PRODUCT_TIER_NAMES:
                findings.append(
                    Branch(
                        "tier-setting",
                        f"{_rel(CONFIG)}:{statement.lineno}",
                        f"Settings.{target} exposes the rollout tier to application "
                        f"code. The structural guarantee is that this field does not "
                        f"exist (config.py's own docstring).",
                    )
                )
    return findings


def scan_typescript(root: Path) -> list[Branch]:
    """The SPA is application code too, and there is no TS parser here.

    Line-based and narrow: a line must both read as a conditional and name a tier.
    Comment lines are dropped first, so prose about tiers cannot trip it — but this
    is a weaker instrument than the Python scan, and it says so rather than
    implying equal rigour.
    """
    findings: list[Branch] = []
    if not root.is_dir():
        return findings
    for path in sorted([*root.rglob("*.ts"), *root.rglob("*.tsx")]):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith(("//", "*", "/*")):
                continue
            if not _TIER_TOKEN.search(stripped) or not _TS_CONDITIONAL.search(stripped):
                continue
            if any(name in stripped.lower() for name in PRODUCT_TIER_NAMES):
                continue
            findings.append(
                Branch(
                    "tier-branch",
                    f"{_rel(path)}:{number}",
                    f"conditional mentions a tier: {stripped[:110]}",
                )
            )
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fail on any application branch keyed on the rollout tier."
    )
    parser.add_argument(
        "--include-tests",
        action="store_true",
        help="scan tests/ too — off by default; a test may legitimately parametrise over tiers",
    )
    args = parser.parse_args()

    try:
        findings = scan_settings() + scan_python(BACKEND_SRC) + scan_typescript(FRONTEND_SRC)
        scanned = f"{_rel(BACKEND_SRC)}, frontend src"
        if args.include_tests:
            findings += scan_python(REPO_ROOT / "tests")
            scanned += ", tests"
    except (OSError, SyntaxError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if not findings:
        print(f"no tier-conditional branch — scanned {scanned}")
        return 0

    print(f"TIER BRANCHES — {len(findings)} finding(s)\n", file=sys.stderr)
    for finding in findings:
        print(finding.render(), end="\n\n", file=sys.stderr)
    print(
        "  The tier lives in composition and configuration values (infra C-6). "
        "If one of these is the rep performance band rather than a rollout rung, "
        "add its name to PRODUCT_TIER_NAMES.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
