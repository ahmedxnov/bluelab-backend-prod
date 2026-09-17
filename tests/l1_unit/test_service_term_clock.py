"""Service dates and retention use the confirmed organization's civil calendar."""

from datetime import UTC, date, datetime

import pytest

from bluelab.platform.clock import (
    resolve_local_time,
    retention_deadline,
    service_term_bounds,
)


def test_last_access_date_remains_accessible_until_next_local_midnight() -> None:
    start, cutoff = service_term_bounds(date(2026, 7, 15), date(2026, 8, 15), "Africa/Cairo")
    assert start == datetime(2026, 7, 14, 21, tzinfo=UTC)
    assert cutoff == datetime(2026, 8, 15, 21, tzinfo=UTC)
    assert start <= datetime(2026, 8, 15, 20, 59, tzinfo=UTC) < cutoff
    assert retention_deadline(cutoff, 90, "elapsed_days", None) == datetime(
        2026, 11, 13, 21, tzinfo=UTC
    )


def test_calendar_months_clamp_to_last_valid_day() -> None:
    assert retention_deadline(
        datetime(2025, 1, 31, tzinfo=UTC), 1, "calendar_months", "UTC"
    ) == datetime(2025, 2, 28, tzinfo=UTC)


def test_elapsed_and_calendar_days_differ_across_clock_change() -> None:
    cutoff = datetime(2026, 3, 7, 5, tzinfo=UTC)  # New York midnight.
    assert retention_deadline(cutoff, 2, "elapsed_days", None) == datetime(
        2026, 3, 9, 5, tzinfo=UTC
    )
    assert retention_deadline(cutoff, 2, "calendar_days", "America/New_York") == datetime(
        2026, 3, 9, 4, tzinfo=UTC
    )


def test_ambiguous_and_nonexistent_local_times() -> None:
    assert resolve_local_time(datetime(2026, 11, 1, 1, 30), "America/New_York") == datetime(  # noqa: DTZ001
        2026, 11, 1, 6, 30, tzinfo=UTC
    )
    assert resolve_local_time(datetime(2026, 3, 8, 2, 30), "America/New_York") == datetime(  # noqa: DTZ001
        2026, 3, 8, 7, tzinfo=UTC
    )


def test_invalid_term_and_retention_inputs() -> None:
    with pytest.raises(ValueError):
        service_term_bounds(date(2026, 8, 15), date(2026, 7, 15), "UTC")
    with pytest.raises(ValueError):
        retention_deadline(datetime(2026, 1, 1, tzinfo=UTC), 1, "calendar_days", None)
