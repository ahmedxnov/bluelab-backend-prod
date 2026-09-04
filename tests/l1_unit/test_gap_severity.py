"""FR-TRM-004's severity thresholds, asserted AT the thresholds.

`test_team_dashboard.py` covers severity end to end, but only at the values the
fixture happens to produce — 2.3, 1.4 and 0.6. Every one of those is comfortably
inside its band, so `>` and `>=` agree on all three and the criterion's actual
wording ("≥ 2.0 high, ≥ 1.0 moderate") goes untested.

The boundary is the whole claim. A gap of exactly 2.0 is high, and an
implementation that used `>` would report it as moderate — one band quieter than
the truth, on the call type most in need of attention. That is a one-character
edit no integration test in this repo would catch.

Pure, so it runs in the unit job without a database.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from bluelab.modules.training.service import HIGH_GAP, MODERATE_GAP, _severity

pytestmark = [pytest.mark.l1_unit]


def test_the_thresholds_are_the_ones_the_criterion_names() -> None:
    """Pins the constants themselves, so the cases below cannot drift with them.

    Every other test here compares against `HIGH_GAP` and `MODERATE_GAP` only
    indirectly. If someone retuned them to 2.5 and 1.5, the boundary cases would
    follow along and keep passing while the product no longer matched FR-TRM-004.
    """
    assert (HIGH_GAP, MODERATE_GAP) == (2.0, 1.0)


@pytest.mark.parametrize(
    ("gap", "expected"),
    [
        (2.1, "high"),
        (2.0, "high"),  # the boundary — `>` would say moderate
        (1.9, "moderate"),
        (1.1, "moderate"),
        (1.0, "moderate"),  # the boundary — `>` would say none
        (0.9, "none"),
        (0.0, "none"),
    ],
)
def test_severity_bands_are_inclusive_at_their_lower_edge(gap: float, expected: str) -> None:
    assert _severity(gap) == expected


def test_a_null_gap_is_unmarked_rather_than_an_error() -> None:
    """A gap is null whenever one cohort has no attempts on that call type.

    Ordinary, not exceptional — so this is a live path. In SQL `null >= 2.0` is
    null and falls through quietly; in Python the same comparison raises
    `TypeError`, which would surface as a 500 on a perfectly normal month.
    """
    assert _severity(None) == "none"


def test_a_negative_gap_is_unmarked() -> None:
    """The bottom half outscoring the top performers on one call type.

    Unusual but real — the cohorts are fixed by OVERALL rating, so a top
    performer can still be the team's weakest at renewals. It is not a coaching
    gap in the direction this surface reports, and marking it would invert the
    meaning of the column.
    """
    assert _severity(-1.5) == "none"


def test_a_decimal_gap_bands_identically_to_a_float() -> None:
    """`numeric` reaches this as `Decimal`, which is what production passes.

    Worth its own case because `Decimal("2.0") >= 2.0` and `float` conversion are
    two different comparisons, and only one of them is exercised by the
    parametrised floats above.
    """
    assert _severity(Decimal("2.0")) == "high"
    assert _severity(Decimal("1.0")) == "moderate"
    assert _severity(Decimal("0.9")) == "none"
