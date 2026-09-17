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
from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal
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


RetentionUnit = Literal["elapsed_days", "calendar_days", "calendar_months"]


def resolve_local_time(local: datetime, timezone: str) -> datetime:
    """Resolve a local civil time to UTC, choosing the later offset on a fold.

    During a forward clock jump, use the first valid instant after the missing
    local time. A skipped whole date has no such instant and is rejected.
    """
    if local.tzinfo is not None:
        raise ValueError("local time must be naive")
    zone = _zone(timezone)
    candidates = [local.replace(tzinfo=zone, fold=fold).astimezone(UTC) for fold in (0, 1)]
    valid = [
        candidate
        for candidate in candidates
        if candidate.astimezone(zone).replace(tzinfo=None) == local
    ]
    if valid:
        return max(valid)

    lower, upper = sorted(candidates)
    # A missing local time lies in the forward gap bounded by the two folds.
    # Search for its first valid UTC microsecond, including unusual non-hour gaps.
    if upper.astimezone(zone).replace(tzinfo=None) < local:
        raise ValueError("local time has no valid successor on this date")
    while upper - lower > timedelta(microseconds=1):
        midpoint = lower + (upper - lower) / 2
        if midpoint.astimezone(zone).replace(tzinfo=None) >= local:
            upper = midpoint
        else:
            lower = midpoint
    if upper.astimezone(zone).date() != local.date():
        raise ValueError("local date has no valid instant")
    return upper


def service_term_bounds(
    start_on: date, last_access_on: date, timezone: str
) -> tuple[datetime, datetime]:
    """Inclusive local dates become a half-open UTC access interval."""
    if last_access_on < start_on:
        raise ValueError("last access date precedes start date")
    starts_at = resolve_local_time(datetime.combine(start_on, datetime.min.time()), timezone)
    ends_at = resolve_local_time(
        datetime.combine(last_access_on + timedelta(days=1), datetime.min.time()),
        timezone,
    )
    if ends_at <= starts_at:
        raise ValueError("service term has no access interval")
    return starts_at, ends_at


def retention_deadline(
    cutoff: datetime, period_value: int, period_unit: RetentionUnit, timezone: str | None
) -> datetime:
    """Resolve a contractual retention period from its actual UTC cutoff."""
    if cutoff.tzinfo is None or period_value < 1:
        raise ValueError("cutoff must be aware and period must be positive")
    cutoff = cutoff.astimezone(UTC)
    if period_unit == "elapsed_days":
        if timezone is not None:
            raise ValueError("elapsed-day retention has no calendar timezone")
        return cutoff + timedelta(days=period_value)
    if timezone is None:
        raise ValueError("calendar retention requires a timezone")
    local = cutoff.astimezone(_zone(timezone)).replace(tzinfo=None)
    if period_unit == "calendar_days":
        target = local + timedelta(days=period_value)
    elif period_unit == "calendar_months":
        month_index = local.year * 12 + local.month - 1 + period_value
        year, zero_month = divmod(month_index, 12)
        month = zero_month + 1
        target = local.replace(
            year=year, month=month, day=min(local.day, monthrange(year, month)[1])
        )
    else:
        raise ValueError("unknown retention period unit")
    return resolve_local_time(target, timezone)


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        # ZoneInfoNotFoundError subclasses KeyError; ValueError covers a
        # malformed key. Catching bare Exception here would also swallow an
        # OSError from a broken tzdata install, which is an ops problem wearing
        # a validation error's clothes.
        raise ValueError(f"unknown timezone: {name!r}") from exc
