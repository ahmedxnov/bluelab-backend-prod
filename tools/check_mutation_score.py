#!/usr/bin/env python3
"""Gate the build on surviving invariant-path mutants (quality/01 §4, pipeline/02 §2 row 2).

`mutmut run` produces the mutants; `mutmut export-cicd-stats` writes the tally to
`mutants/mutmut-cicd-stats.json`. This reads that tally and decides whether the
build may proceed.

    mutmut run
    mutmut export-cicd-stats
    python tools/check_mutation_score.py                    # commit gate stage 2
    python tools/check_mutation_score.py --update-baseline  # after killing mutants

## Why this exists rather than just trusting mutmut's exit code

mutmut exits non-zero whenever *any* mutant survives, and on a part-built product
that is every run — T-7 has no test at all, so every mutant in `invites.py`
survives by construction. A gate that is red on every commit is one the team
learns to merge through, which is worse than no gate. So the surviving count
ratchets: it may fall freely, it may never rise.

## Why a count, and not a named set like the other two ratchets

`check_conformance_diff` and `check_requirement_coverage` baseline a *set* of named
gaps, because "operation:GET /api/v1/positions" means the same thing next week.
A mutant has no such stable name: mutmut identifies it by source file plus an
ordinal within the file, so inserting a line renumbers every mutant below it. A
named-set baseline would churn on every unrelated edit and teach everyone to
regenerate it without reading — which is precisely how a baseline stops meaning
anything. The count is the part that is stable and the part that matters.

The cost is honestly stated: a count cannot tell "killed one, introduced another"
from "nothing changed". `mutmut browse` is the tool for that, and the per-file
breakdown printed here narrows where to look.

## Exit codes

* **0** — no more survivors than the baseline allows.
* **1** — the gate is red: survivors rose, or the baseline is now slack.
* **2** — the check could not run: no stats file, an unreadable or unreconcilable
  one, no mutants at all, or a damaged baseline. Never 0: a mutation gate that
  passes without having run any mutants is the emptiest false green available.
  Never 1 either — "could not run" and "is red" send the reader to different
  places, and collapsing them points them at the wrong one.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _ratchet

STATS = REPO_ROOT / "mutants" / "mutmut-cicd-stats.json"
BASELINE = _ratchet.BASELINE_DIR / "check-mutation-score.json"


def _rel(path: Path) -> str:
    """A repo-relative label, falling back to the absolute path.

    `relative_to` raises for anything outside the tree, and a crash while
    formatting a success message would turn a passing gate into an error.
    """
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


@dataclass(frozen=True, slots=True)
class Tally:
    """The mutmut CI/CD stats, reduced to the numbers that decide the gate."""

    killed: int
    survived: int
    total: int
    no_tests: int
    timeout: int
    suspicious: int
    skipped: int
    segfault: int
    interrupted: bool

    @property
    def unkilled(self) -> int:
        """Every mutant that did not die — not just the ones mutmut calls survivors.

        mutmut splits non-kills across six statuses. `survived` means the suite ran
        and stayed green. `no_tests` means no test reaches the line at all.
        `timeout` means the suite hung instead of returning a verdict, `suspicious`
        that mutmut could not tell what happened, `segfault` that the process died,
        and `skipped` that the mutant was never attempted.

        All six are folded together because this gate asks exactly one question —
        *would we notice if this were wrong* — and for every one of them the answer
        is no. Counting only `survived` and `no_tests` lets a mutant leave the
        arithmetic by failing to produce a verdict, which is the cheapest way there
        is to raise a mutation score without writing a test: park 54 mutants in
        timeout and the same 100 kills out of the same 160 mutants reads 94.3%
        instead of 62.5%.

        That needs no attacker. `--max-children 1` is mandatory here (the L3 suite
        shares one database), so a loaded runner pushes marginal mutants into
        `timeout` on its own — and the gate would have reported the loss as
        "mutants newly killed, tighten the baseline".
        """
        return (
            self.survived
            + self.no_tests
            + self.timeout
            + self.suspicious
            + self.segfault
            + self.skipped
        )

    @property
    def score(self) -> float:
        """Killed as a fraction of mutants that had a verdict."""
        judged = self.killed + self.unkilled
        return self.killed / judged if judged else 0.0


def _count(raw: dict[str, object], key: str) -> int:
    """One non-negative whole number from the stats file.

    A bare `int(raw[key])` accepts three things a mutant tally cannot contain, and
    each one corrupts the comparison the gate is about to make:

    * a **negative** count, which subtracts real survivors — `survived: -1000`
      yields `unkilled: -990` and a score of -11.2%;
    * a **fractional** count, silently truncated (`50.9` becomes 50);
    * a **boolean**, because `bool` subclasses `int` in Python, so `true` becomes 1
      without complaint.

    None can come out of a real mutmut run, so each means the file is not what it
    claims to be. The caller turns that into exit 2 — could not run — rather than
    gating on a fiction.

    Raises:
        TypeError: The value is not a number at all (including `bool`, which is a
            number in Python but never a count here).
        ValueError: It is a number that a count cannot be — negative, or
            fractional. Both callers catch the pair.
    """
    value = raw[key]
    if isinstance(value, bool):
        raise TypeError(f"{key!r} is a boolean; a mutant count must be a number")
    if not isinstance(value, (int, float)):
        raise TypeError(f"{key!r} is {type(value).__name__}; a mutant count must be a number")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{key!r} is {value}; a mutant count must be a whole number")
    count = int(value)
    if count < 0:
        raise ValueError(f"{key!r} is {count}; a mutant count cannot be negative")
    return count


def load_tally(path: Path) -> Tally:
    """Read mutmut's exported stats.

    Args:
        path: Location of `mutmut-cicd-stats.json`.

    Returns:
        The parsed tally.

    Raises:
        OSError: If the file is absent — meaning `mutmut run` never completed, or
            `mutmut export-cicd-stats` was not called after it.
        ValueError: If the JSON is malformed, a key is missing, a count is not a
            non-negative whole number, the statuses do not account for every
            mutant, or the run reports zero mutants. Zero mutants is not success:
            it means `only_mutate` matched nothing, and the gate would otherwise
            pass having tested nothing at all.
    """
    if not path.exists():
        raise OSError(
            f"{path} not found. Run `mutmut run` then `mutmut export-cicd-stats` "
            f"first — this check reads their output, it does not run them."
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        tally = Tally(
            killed=_count(raw, "killed"),
            survived=_count(raw, "survived"),
            total=_count(raw, "total"),
            no_tests=_count(raw, "no_tests"),
            timeout=_count(raw, "timeout"),
            suspicious=_count(raw, "suspicious"),
            skipped=_count(raw, "skipped"),
            segfault=_count(raw, "segfault"),
            interrupted=bool(raw["check_was_interrupted_by_user"]),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{path} is not a mutmut stats file: {exc}") from exc

    if tally.total == 0:
        raise ValueError(
            f"{path} reports 0 mutants. `only_mutate` in pyproject.toml matched no "
            f"file, so this gate would pass having tested nothing. Check the "
            f"patterns — they are fnmatch globs against POSIX paths."
        )
    if tally.killed + tally.unkilled == 0:
        raise ValueError(
            f"{path} reports {tally.total} mutants but no verdict on any of them. "
            f"The run generated mutants and then never executed them — a collection "
            f"error in the selected suite is the usual cause, and mutmut still "
            f"writes a stats file afterwards. Run `mutmut run` directly and read "
            f"the pytest output."
        )
    if tally.interrupted:
        raise ValueError(
            "the mutmut run was interrupted, so the tally is partial and every "
            "unreached mutant looks killed. Refusing to gate on it."
        )
    if tally.killed + tally.unkilled != tally.total:
        # The statuses must account for every mutant. They do not here, which means
        # either the file was not written by mutmut, or mutmut has grown a status
        # this code does not know about — and an unknown status is a bucket mutants
        # can sit in while the gate ignores them. Refusing is what keeps `unkilled`
        # honest as "everything that did not die" rather than "the parts we listed".
        raise ValueError(
            f"{path} does not reconcile: {tally.killed} killed + {tally.unkilled} "
            f"unkilled != {tally.total} total. Either this is not a mutmut stats "
            f"file, or mutmut reports a status this gate does not count — check its "
            f"output against the fields in `Tally`."
        )
    return tally


def load_baseline() -> int | None:
    """The highest survivor count this gate currently tolerates, or None if unset.

    Raises:
        ValueError: If the file exists but is not a baseline this gate can read —
            malformed JSON, no `unkilled` key, or a value that is not a
            non-negative whole number.

    An absent file is not an error; it returns None and `main` refuses to run. A
    *corrupt* one is, and it is deliberately not silently treated as absent: that
    would convert a damaged baseline into "no baseline", and the ceiling would then
    be rebuilt from whatever the next `--update-baseline` happened to find.
    """
    if not BASELINE.exists():
        return None
    try:
        payload = json.loads(BASELINE.read_text(encoding="utf-8"))
        return _count(payload, "unkilled")
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{BASELINE} is not a readable baseline: {exc}") from exc


def write_baseline(tally: Tally) -> Path:
    BASELINE.parent.mkdir(parents=True, exist_ok=True)
    BASELINE.write_text(
        json.dumps(
            {
                "note": (
                    "Surviving invariant-path mutants (quality/01 §4). A surviving "
                    "mutant is a missing test. This number may only go down."
                ),
                "unkilled": tally.unkilled,
                "survived": tally.survived,
                "no_tests": tally.no_tests,
                "killed": tally.killed,
                "score": round(tally.score, 4),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return BASELINE


def summarise(tally: Tally) -> str:
    parts = [
        f"{tally.killed} killed",
        f"{tally.survived} survived",
        f"{tally.no_tests} with no test",
        f"score {tally.score:.1%}",
    ]
    for label, value in (
        ("timeout", tally.timeout),
        ("suspicious", tally.suspicious),
        ("skipped", tally.skipped),
        ("segfault", tally.segfault),
    ):
        if value:
            parts.append(f"{value} {label}")
    return ", ".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Gate on surviving invariant-path mutants (quality/01 §4)."
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="record today's survivor count as the ceiling. It may only go down.",
    )
    parser.add_argument("--stats", type=Path, default=STATS, help="path to mutmut-cicd-stats.json")
    args = parser.parse_args()

    try:
        tally = load_tally(args.stats)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(f"  {summarise(tally)}")
    sys.stdout.flush()

    if args.update_baseline:
        target = write_baseline(tally)
        print(f"\nwrote {_rel(target)} — ceiling {tally.unkilled}")
        return 0

    try:
        allowed = load_baseline()
    except (OSError, ValueError) as exc:
        # 2, not 1. A damaged baseline means the check could not run; reported as 1
        # it reads as "the gate is red", sends the reader hunting for surviving
        # mutants, and arrives as a raw traceback. `check_conformance_diff` makes
        # the same distinction for the same reason.
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if allowed is None:
        print(
            "\nERROR: no baseline recorded. A mutation gate that starts life "
            "accepting whatever it finds forgives the entire backlog on its first "
            "run and never mentions it again.\n"
            "Fix: python tools/check_mutation_score.py --update-baseline",
            file=sys.stderr,
        )
        return 1

    if tally.unkilled > allowed:
        print(
            f"\nMUTANTS SURVIVED — {tally.unkilled} unkilled, baseline allows "
            f"{allowed}. {tally.unkilled - allowed} more than before: a surviving "
            f"mutant is a missing test (quality/01 §4).\n"
            f"  Inspect with: mutmut browse",
            file=sys.stderr,
        )
        return 1

    if tally.unkilled < allowed:
        print(
            f"\n{allowed - tally.unkilled} mutant(s) newly killed — the baseline has "
            f"gone slack at {allowed} and would now forgive a regression back up to "
            f"it.\n"
            f"Fix: python tools/check_mutation_score.py --update-baseline",
            file=sys.stderr,
        )
        return 1

    print(f"\nmutation bar holds — {tally.unkilled} unkilled, at the baseline")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
