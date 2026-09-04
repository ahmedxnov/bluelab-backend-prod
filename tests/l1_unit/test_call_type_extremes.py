"""A rep's strongest and weakest call types (FR-TRM-005).

`test_team_roster.py` covers this end to end, but only against the fixture's
shape: five reps who each measured three or four types, with a distinct maximum
and minimum every time. Three states it cannot reach live here — nothing
measured, exactly one measured, and a tie at either extreme.

The tie case matters most. `max` and `min` both return the FIRST extreme they
meet, so the answer depends on the order the caller built the mapping in. That is
fine, and it is fixed — but it is invisible, and an edit that rebuilt the mapping
from a set or a `**kwargs` would change which call type a rep is told to work on
without failing anything.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from bluelab.modules.training.service import _extremes

pytestmark = [pytest.mark.l1_unit]


def test_nothing_measured_is_two_nulls() -> None:
    """A rep with no counted attempts has no best and no worst.

    Not "their weakest is whichever they have never tried" — an untried call type
    is not a weakness, and treating a null as zero would make `weakest` mean
    `least practised` on every new rep's row.
    """
    assert _extremes(
        {"discovery": None, "post_proposal": None, "renewal": None, "upsell": None}
    ) == (None, None)


def test_one_measured_type_is_both_the_best_and_the_worst() -> None:
    """Honest rather than tidy.

    It reads oddly on a page — strongest and weakest naming the same thing — but
    it is true, and the alternative hides the more useful fact: this rep has
    practised exactly one kind of call.
    """
    assert _extremes(
        {"discovery": None, "post_proposal": 6.4, "renewal": None, "upsell": None}
    ) == ("post_proposal", "post_proposal")


def test_untried_types_do_not_become_the_weakest() -> None:
    """The null-versus-zero distinction, at the place it would do damage.

    Upsell is unmeasured and renewal is measured at 6.3. Treating the null as a
    zero would report upsell — a call type this rep has never taken — as the thing
    they are worst at.
    """
    assert _extremes(
        {"discovery": 9.0, "post_proposal": 8.2, "renewal": 6.3, "upsell": None}
    ) == ("discovery", "renewal")


def test_a_tie_at_the_top_resolves_to_the_first_call_type() -> None:
    """Arbitrary, but fixed — two renders of one month must not disagree.

    The service builds this mapping in `CALL_TYPES` order, so the first extreme
    `max` meets is the earlier call type. Pinned here because nothing else would
    notice it changing.
    """
    assert _extremes(
        {"discovery": 8.0, "post_proposal": 8.0, "renewal": 6.0, "upsell": 7.0}
    )[0] == "discovery"


def test_a_tie_at_the_bottom_resolves_to_the_first_call_type() -> None:
    assert _extremes(
        {"discovery": 9.0, "post_proposal": 7.0, "renewal": 5.0, "upsell": 5.0}
    )[1] == "renewal"


def test_all_four_equal_names_the_first_for_both() -> None:
    """The degenerate tie. Consistent, and deliberately not two different types."""
    assert _extremes(
        {"discovery": 7.0, "post_proposal": 7.0, "renewal": 7.0, "upsell": 7.0}
    ) == ("discovery", "discovery")


def test_decimals_compare_as_numbers_not_as_objects() -> None:
    """`numeric` reaches this as `Decimal`, which is what production passes.

    Worth its own case: a comparison that fell back to object ordering, or that
    mixed `Decimal` and `float` without converting, would still return *something*
    for every input above.
    """
    assert _extremes(
        {
            "discovery": Decimal("6.9"),
            "post_proposal": Decimal("7.10"),
            "renewal": Decimal("6.90"),
            "upsell": None,
        }
    ) == ("post_proposal", "discovery")
