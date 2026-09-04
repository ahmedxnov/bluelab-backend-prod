"""Time, and org-local calendar semantics.

Storage is `timestamptz`, UTC, everywhere (data/00 §2). Calendar-month buckets
and due dates resolve in `org.timezone`, server-side (data/02 §3 V-1, api/00 §2)
— a client never performs this arithmetic.

The distinction this module exists to keep straight:

* an **instant** is a point on the timeline — always UTC, always `timestamptz`;
* a **calendar month** is a span the org lived through, and "May" starts and ends
  at different instants in Cairo than in UTC.

FR-TRM-002's "calendar month" is the second kind. Getting it wrong shifts every
attempt taken in the first three hours of a Cairo month into the previous
month's rating, which is a wrong number on a coaching dashboard rather than a
crash — the worst kind of bug to ship.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_MONTH_PATTERN = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


def now() -> datetime:
    """The current instant, UTC and timezone-aware.

    Never use `datetime.utcnow()`: it returns a naive value that silently
    compares wrong against the timezone-aware values the database returns.
    """
    return datetime.now(tz=UTC)


@dataclass(frozen=True, slots=True)
class MonthWindow:
    """A calendar month as a half-open instant range: `[start, end)`.

    Half-open because the alternative — an inclusive end — needs a "last
    microsecond" value that differs by column precision and eventually admits or
    drops a row at the boundary. `start <= t < end` has no such edge.
    """

    label: str
    """The `YYYY-MM` label, as the API accepts and returns it (api/00 §2)."""

    timezone: str
    """The IANA zone the window was resolved in — the org's, never the server's."""

    start: datetime
    """First instant of the month, UTC."""

    end: datetime
    """First instant of the *next* month, UTC. Exclusive."""

    def contains(self, instant: datetime) -> bool:
        """True if `instant` falls inside this month, org-locally."""
        return self.start <= instant < self.end


def parse_month(label: str) -> tuple[int, int]:
    """Parse a `YYYY-MM` month parameter.

    Args:
        label: A month string as it arrives on the wire, e.g. `"2026-05"`.

    Returns:
        The `(year, month)` pair.

    Raises:
        ValueError: If `label` is not a well-formed `YYYY-MM` string.
    """
    match = _MONTH_PATTERN.match(label)
    if match is None:
        raise ValueError(f"month must be YYYY-MM, got {label!r}")
    return int(match.group(1)), int(match.group(2))


def month_window(label: str, org_timezone: str) -> MonthWindow:
    """Resolve a `YYYY-MM` label into a UTC instant range, org-locally.

    This is the single implementation of "the month the org lived" (data/02 §3
    V-1). Every monthly aggregate — rep rating, team month, gap analysis, trend
    against the calendar-prior month — bounds on a window produced here.

    Args:
        label: `YYYY-MM`.
        org_timezone: The org's IANA timezone, e.g. `"Africa/Cairo"`.

    Returns:
        The half-open window covering that month in that zone.

    Raises:
        ValueError: If the label is malformed or the timezone is unknown.
    """
    year, month = parse_month(label)
    zone = _zone(org_timezone)

    local_start = datetime(year, month, 1, tzinfo=zone)
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
    local_end = datetime(next_year, next_month, 1, tzinfo=zone)

    return MonthWindow(
        label=label,
        timezone=org_timezone,
        start=local_start.astimezone(UTC),
        end=local_end.astimezone(UTC),
    )


def prior_month_window(window: MonthWindow) -> MonthWindow:
    """The calendar-prior month, in the same zone.

    V-3's trend compares against the *calendar*-prior month, not "30 days ago" —
    a distinction that matters in February and after any DST shift.
    """
    year, month = parse_month(window.label)
    prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
    return month_window(f"{prev_year:04d}-{prev_month:02d}", window.timezone)


def current_month_label(org_timezone: str, *, at: datetime | None = None) -> str:
    """The `YYYY-MM` label of the month the org is currently in."""
    instant = at or now()
    local = instant.astimezone(_zone(org_timezone))
    return f"{local.year:04d}-{local.month:02d}"


def due_date_deadline(due: date, org_timezone: str) -> datetime:
    """The instant a due date stops being met, org-locally.

    A due date is a *calendar* date on the wire (api/00 §2). "Due 22 May" means
    the end of 22 May where the org is, so the deadline is the first instant of
    23 May in the org's zone — the same half-open convention as `MonthWindow`.

    FR-TRM-002's "completed by due date" support metric counts on this boundary;
    a UTC reading would mark late anything finished in the last two Cairo hours
    of the day.
    """
    zone = _zone(org_timezone)
    next_day = due + timedelta(days=1)
    return datetime(next_day.year, next_day.month, next_day.day, tzinfo=zone).astimezone(UTC)


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        # ZoneInfoNotFoundError subclasses KeyError; ValueError covers a
        # malformed key. Catching bare Exception here would also swallow an
        # OSError from a broken tzdata install, which is an ops problem wearing
        # a validation error's clothes.
        raise ValueError(f"unknown timezone: {name!r}") from exc
