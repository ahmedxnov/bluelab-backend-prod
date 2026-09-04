"""A ratchet for gates that must be usable before the thing they gate is finished.

Two of the build-gate checks assert **completeness** — every requirement has a
test (ADR-0062), every contracted operation is served (ADR-0035). Read literally
against a part-built product they are red on day one and stay red until v1 ships,
and a gate that is always red is a gate everybody learns to ignore. That is worse
than no gate: it trains the team to merge through a wall of failures.

So each records a **baseline** — the gaps that exist today — and fails on:

* **any new gap**, which is a regression, and
* **any gap the baseline still lists that no longer exists**, which is the baseline
  gone slack.

The second half is what makes this a ratchet rather than a permanent excuse list.
Without it the file only ever grows stale, and every gap it names stays forgiven
forever — including one that gets *re-introduced* after being fixed. Closing a gap
therefore fails the build once, and `--update-baseline` is the one-command fix.
That friction is deliberate and it is small: it buys a file whose length is an
honest measure of how much is left.

**Contradictions are never ratchetable.** A server serving a path the contract does
not define, or a problem type whose status disagrees with the catalog, is not an
absence — it is code and specification actively disagreeing, and no baseline
forgives it. Each check decides which of its findings are gaps and which are
contradictions; this module only handles the gaps.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

BASELINE_DIR = Path(__file__).resolve().parent / "baselines"


@dataclass(frozen=True, slots=True)
class Verdict:
    """The outcome of comparing today's gaps against the recorded ones."""

    new: tuple[str, ...]
    """Gaps that appeared. A regression — the build fails."""

    resolved: tuple[str, ...]
    """Gaps the baseline still forgives that are now closed. Tighten the file."""

    still_open: tuple[str, ...]
    """Known and accepted. Reported as a count, never as a failure."""

    @property
    def clean(self) -> bool:
        return not self.new and not self.resolved


def path_for(name: str) -> Path:
    return BASELINE_DIR / f"{name}.json"


def load(name: str) -> frozenset[str]:
    """The recorded gaps. A missing file is an empty baseline, not an error.

    An absent file means "nothing is forgiven", so a first run reports every gap as
    new and fails — which is the correct introduction for a ratchet. It must never
    silently start life forgiving everything it happens to find.
    """
    target = path_for(name)
    if not target.exists():
        return frozenset()
    payload = json.loads(target.read_text(encoding="utf-8"))
    return frozenset(payload["gaps"])


def evaluate(name: str, gaps: set[str]) -> Verdict:
    baseline = load(name)
    return Verdict(
        new=tuple(sorted(gaps - baseline)),
        resolved=tuple(sorted(baseline - gaps)),
        still_open=tuple(sorted(gaps & baseline)),
    )


def write(name: str, gaps: set[str], *, note: str) -> Path:
    """Record today's gaps as the accepted baseline.

    Sorted and one per line so a reviewer reads the diff as "these three closed,
    this one opened" rather than as a reordered blob. The file is meant to be read
    in review — it is the list of what this gate is currently forgiving.

    Args:
        name: Baseline identifier, used as the filename stem.
        gaps: The full current gap set. Replaces the file's contents rather than
            merging into them, so a closed gap actually disappears.
        note: One line written into the file explaining what it forgives, for the
            reader who opens it without this module to hand.

    Returns:
        The path written.

    Raises:
        OSError: If the baselines directory cannot be created or written.
    """
    target = path_for(name)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "note": note,
        "count": len(gaps),
        "gaps": sorted(gaps),
    }
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return target


def render(name: str, verdict: Verdict, *, noun: str) -> list[str]:
    """The reportable lines. Empty when the ratchet is satisfied."""
    lines: list[str] = []
    if verdict.new:
        lines.append(f"{len(verdict.new)} new {noun} — this is a regression:")
        lines.extend(f"    + {gap}" for gap in verdict.new)
    if verdict.resolved:
        lines.append(
            f"{len(verdict.resolved)} {noun} in the baseline are now closed. "
            f"The baseline has gone slack — every entry it still names stays "
            f"forgiven, including one that gets re-introduced later:"
        )
        lines.extend(f"    - {gap}" for gap in verdict.resolved)
    if lines:
        lines.append(f"Fix: python tools/{name.replace('-', '_')}.py --update-baseline")
    return lines
