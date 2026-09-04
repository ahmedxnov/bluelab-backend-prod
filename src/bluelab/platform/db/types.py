"""Shared column types: the score type, and the frozen-JSONB snapshot type with its
mandatory `"v"` version field, whose readers must accept every historical version
forever (data/04 §5, ADR-0032).

## The score type

`numeric(3,1)`, `CHECK (… between 0 and 10)`. Not a float: a float cannot
represent 7.5 exactly, and NFR-004's tolerance is ±0.5 overall and ±1.0 per
dimension — a variance study whose measurement error comes from the storage type
is not a study. `Decimal` all the way to the wire, one decimal place.

## The snapshot type

Frozen snapshots — `drill.scenario`, `drill.answer_key` — are read whole and
never queried into, so they are JSONB blobs with no GIN index (data/00 §7).

Every snapshot carries `"v": <int>`. Because frozen rows are **never rewritten**
(FR-DRL-015), snapshot evolution is reader-side only: a format change bumps `v`
for newly published drills and there is no backfill, ever. Which means every
reader — brief, product reference, grading, PDF — must accept every historical
`v` **forever**, and a fixture set holding one example per version proves it in
CI (data/04 §5).

`SnapshotReader` exists to make that obligation mechanical rather than
remembered: registering a reader per version means an unhandled version raises a
named error instead of a `KeyError` three frames deep.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import CheckConstraint, Numeric, SmallInteger
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import TypeEngine

SCORE_TYPE: Final[TypeEngine[Decimal]] = Numeric(precision=3, scale=1)
"""`numeric(3,1)` — 0.0 … 10.0, one decimal (data/00 §2)."""

WEIGHT_TYPE: Final[TypeEngine[int]] = SmallInteger()
"""Rubric weights are integers, matching the "+3 to balance" gate (AC-DRL-003).

Rubric weights **only**. For other small integers use `SMALLINT_TYPE` — the two
are the same storage but not the same idea, and a column typed `WEIGHT_TYPE`
should be something the sum-to-100 rule applies to.
"""

SMALLINT_TYPE: Final[TypeEngine[int]] = SmallInteger()
"""A plain `smallint` — openings, attempts allowed, invite expiry days.

Distinct from `WEIGHT_TYPE` for readability, not for storage.
"""

SNAPSHOT_TYPE: Final[TypeEngine[dict[str, Any]]] = JSONB()
"""Frozen snapshots only. Never for data that is queried into, joined, or
mutated field-wise (data/00 §2)."""

SCORE_MIN: Final = Decimal("0.0")
SCORE_MAX: Final = Decimal("10.0")

SNAPSHOT_VERSION_KEY: Final = "v"


def score_check(column: str) -> CheckConstraint:
    """`CHECK (column between 0 and 10)` — the cheap backstop under the app gate."""
    return CheckConstraint(f"{column} >= 0 and {column} <= 10", name=f"{column}_range")


def weight_check(column: str) -> CheckConstraint:
    """A rubric weight is a non-negative integer no greater than 100.

    Sum-to-100 itself is an application-transaction gate (T-5), not a row
    constraint — a single row cannot see its siblings. The DDL carries only the
    cheap backstop, per data/00 §7.
    """
    return CheckConstraint(f"{column} >= 0 and {column} <= 100", name=f"{column}_range")


def quantize_score(value: Decimal | float) -> Decimal:
    """Round to the one decimal place the column and the API both use.

    Args:
        value: A raw score.

    Returns:
        The value as a `Decimal` with exactly one decimal place.

    Raises:
        ValueError: If the value falls outside 0…10.
    """
    result = Decimal(str(value)).quantize(Decimal("0.1"))
    if not (SCORE_MIN <= result <= SCORE_MAX):
        raise ValueError(f"score out of range: {result}")
    return result




class UnknownSnapshotVersion(Exception):
    """A frozen snapshot carries a version no reader handles.

    This is always a defect and never a data problem: frozen rows are immutable,
    so every version that has ever been written still exists and must still be
    readable. Deleting a reader is the bug.
    """

    def __init__(self, version: int, known: tuple[int, ...]) -> None:
        super().__init__(f"snapshot version {version} has no reader (known: {known})")
        self.version = version
        self.known = known


class SnapshotReader[T]:
    """Version-dispatching reader for a frozen JSONB snapshot.

    Register one reader per version. Never delete one — that is the whole point.

    Example:
        scenario = SnapshotReader[Scenario]("drill.scenario")

        @scenario.version(1)
        def _v1(payload: dict[str, Any]) -> Scenario: ...

        @scenario.version(2)
        def _v2(payload: dict[str, Any]) -> Scenario: ...
    """

    def __init__(self, name: str) -> None:
        self._name = name
        self._readers: dict[int, Callable[[dict[str, Any]], T]] = {}

    def version(self, number: int) -> Callable[[Callable[[dict[str, Any]], T]], Callable[[dict[str, Any]], T]]:
        """Register the reader for snapshot version `number`."""

        def decorate(fn: Callable[[dict[str, Any]], T]) -> Callable[[dict[str, Any]], T]:
            if number in self._readers:
                raise ValueError(f"{self._name}: version {number} already has a reader")
            self._readers[number] = fn
            return fn

        return decorate

    @property
    def known_versions(self) -> tuple[int, ...]:
        """Every version this reader handles — the CI fixture set's expectation."""
        return tuple(sorted(self._readers))

    @property
    def current_version(self) -> int:
        """The highest registered version — what a new snapshot is written as."""
        if not self._readers:
            raise ValueError(f"{self._name}: no readers registered")
        return max(self._readers)

    def read(self, payload: dict[str, Any]) -> T:
        """Dispatch on the snapshot's `"v"` and read it.

        Raises:
            UnknownSnapshotVersion: If no reader handles the payload's version.
            ValueError: If the payload carries no version at all — an unversioned
                snapshot is unreadable forever, so it must never be written.
        """
        raw = payload.get(SNAPSHOT_VERSION_KEY)
        if raw is None:
            raise ValueError(f"{self._name}: snapshot has no {SNAPSHOT_VERSION_KEY!r} field")
        version = int(raw)
        reader = self._readers.get(version)
        if reader is None:
            raise UnknownSnapshotVersion(version, self.known_versions)
        return reader(payload)
