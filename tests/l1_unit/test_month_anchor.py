"""`YYYY-MM` → the value `v_counted_attempt.local_month` actually holds.

Every month-scoped surface — the rep's progress, and now the manager's dashboard,
roster, deep dive and cohorts — compares a caller-supplied month against
`local_month`, which V-1 computes as:

    date_trunc('month', a.started_at at time zone o.timezone)

`at time zone` applied to a `timestamptz` yields a **naive** `timestamp` in the
org's civil calendar, and `date_trunc` pins it to the first of the month at
midnight. So the comparison value must be naive too. Attaching UTC to it would
not fail loudly: postgres would compare a `timestamp` against a `timestamptz` by
coercing one of them through the session `TimeZone`, and the answer would be
right for orgs at UTC and silently wrong by one month at every boundary for
everybody else — the Cairo rep and the Frankfurt process disagreeing, which is
the whole reason the derivation is org-local in the first place.

That is why `test_the_result_is_naive` exists as its own case rather than as an
extra assert: it is the property, and the rest is arithmetic.

The contract's pattern (`Month` in `modules.training.schemas`) is the first pass
at the boundary, but it is NOT sufficient: `^[0-9]{4}-...` admits `0000-01`, and
Python's `MINYEAR` is 1. A caller who did everything the contract asks could still
reach a `ValueError` here, and an uncaught one renders as `500 internal-error` —
a server fault on the dashboards for what is only bad input. So `parse_month`
validates, and the cases below pin that.
"""

from __future__ import annotations

import re
from datetime import datetime

import pytest

from bluelab.modules.training.schemas import MONTH_PATTERN
from bluelab.modules.training.service import parse_month
from bluelab.platform.errors import catalog
from bluelab.platform.errors.denial import ValidationProblem

pytestmark = [pytest.mark.l1_unit]


def test_parses_to_midnight_on_the_first() -> None:
    assert parse_month("2026-08") == datetime(2026, 8, 1, 0, 0, 0)  # noqa: DTZ001


def test_the_result_is_naive() -> None:
    """The property the whole surface rests on — see the module docstring."""
    assert parse_month("2026-08").tzinfo is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-01", datetime(2026, 1, 1)),  # noqa: DTZ001
        ("2026-12", datetime(2026, 12, 1)),  # noqa: DTZ001
        ("1999-09", datetime(1999, 9, 1)),  # noqa: DTZ001
        ("2000-02", datetime(2000, 2, 1)),  # noqa: DTZ001
    ],
)
def test_month_boundaries(value: str, expected: datetime) -> None:
    """January and December are where an off-by-one in the parse would show."""
    assert parse_month(value) == expected


@pytest.mark.parametrize("value", ["0000-01", "0000-12"])
def test_year_zero_passes_the_pattern_and_is_still_refused(value: str) -> None:
    """The gap that makes the pattern insufficient, asserted from both sides.

    Matching the contract's own regex is asserted first, because that is the whole
    point: this input is not malformed by the contract's definition, and a reader
    who assumed the pattern was enough would call it unreachable.
    """
    assert re.match(MONTH_PATTERN, value), "premise broken — the pattern now rejects this"

    with pytest.raises(ValidationProblem) as raised:
        parse_month(value)

    assert raised.value.problem is catalog.VALIDATION_ERROR
    assert raised.value.problem.status == 422


def test_the_refusal_names_the_field_without_echoing_the_input() -> None:
    """`errors[]` binds to a control (ux/05 §3.1), so the field name is required.

    The message is fixed rather than derived: `strptime`'s own text ("year 0 is
    out of range") is an implementation detail, and reflecting the caller's input
    back into a response body is a habit worth not starting.
    """
    with pytest.raises(ValidationProblem) as raised:
        parse_month("0000-01")

    assert raised.value.errors == [
        {"field": "month", "message": "Not a calendar month in YYYY-MM form."}
    ]
    assert "0000" not in str(raised.value.meta)
